"""Runtime inference and command-line resolution.

Motivated by real findings on the GX10: several GPU workloads run as a bare
`python` process, so the process name alone identifies nothing. vLLM is
identifiable from argv; ComfyUI only from its working directory.
"""

import pytest
from spark_dash_agent.collectors.gpu import (
    LLM_RUNTIMES,
    _command_line,
    _cwd,
    _num,
    infer_runtime,
)


class TestInferRuntime:
    def test_llama_server_by_name(self):
        assert infer_runtime("llama-server") == "llama.cpp"

    def test_vllm_from_command_line_when_name_is_just_python(self):
        """The GX10 case: vLLM runs as `python`, so identity lives in argv."""
        cmd = "python -m vllm.entrypoints.openai.api_server --model meta-llama/Llama-3.3-70B"
        assert infer_runtime("python", cmd) == "vllm"

    def test_vllm_wins_over_llama_when_serving_a_llama_model(self):
        """A vLLM process serving Llama has 'llama' in argv — checking
        llama.cpp first would misattribute it to the wrong runtime."""
        cmd = "python -m vllm.entrypoints.openai.api_server --model meta-llama/Llama-3.3-70B"
        assert infer_runtime("python", cmd) == "vllm"

    def test_llama_server_serving_a_model_named_vllm_is_not_confused(self):
        # Guards the ordering rule from the opposite direction.
        assert infer_runtime("llama-server", "llama-server -m qwen3.gguf") == "llama.cpp"

    def test_plain_python_stays_unlabeled(self):
        """An unrecognized process still appears in the table, just unlabeled —
        better than a confident wrong guess."""
        assert infer_runtime("python", "python train.py") is None

    def test_unknown_process(self):
        assert infer_runtime("sshd", "/usr/sbin/sshd -D") is None

    @pytest.mark.parametrize(
        "name,cmd,expected",
        [
            ("python", "python -m sglang.launch_server", "sglang"),
            ("text-generation-server", "", "tgi"),
            ("ollama", "ollama serve", "ollama"),
            ("llama-server", "", "llama.cpp"),
        ],
    )
    def test_other_runtimes(self, name, cmd, expected):
        assert infer_runtime(name, cmd) == expected

    def test_case_insensitive(self):
        assert infer_runtime("PYTHON", "-m VLLM.entrypoints") == "vllm"


class TestNonLlmGpuWorkloads:
    """Not every GPU consumer is an inference runtime. On GB10 these share the
    same unified pool as the models, so labeling them is the difference between
    "12GB used, unexplained" and "12GB used by ComfyUI".
    """

    def test_comfyui_identified_by_cwd_alone(self):
        """The GX10 case: ComfyUI runs as `python main.py`, so neither the name
        nor argv names it — only the working directory does."""
        assert infer_runtime("python", "python main.py --listen", "/app/ComfyUI") == "comfyui"

    def test_comfyui_identified_from_argv_path(self):
        assert infer_runtime("python", "python /opt/comfyui/main.py") == "comfyui"

    def test_stable_diffusion_webui(self):
        assert infer_runtime("python", "python launch.py", "/home/u/stable-diffusion-webui") == (
            "stable-diffusion"
        )

    def test_jupyter(self):
        assert infer_runtime("python", "python -m ipykernel_launcher -f kernel.json") == "jupyter"

    def test_bare_python_main_without_cwd_stays_unlabeled(self):
        """With cwd unreadable there is genuinely nothing to go on — better
        unlabeled than guessed."""
        assert infer_runtime("python", "python main.py") is None

    def test_llm_runtimes_set_excludes_non_inference_workloads(self):
        assert "vllm" in LLM_RUNTIMES
        assert "llama.cpp" in LLM_RUNTIMES
        assert "comfyui" not in LLM_RUNTIMES
        assert "stable-diffusion" not in LLM_RUNTIMES
        assert "jupyter" not in LLM_RUNTIMES

    def test_inference_runtime_still_wins_over_workload_match(self):
        """A vLLM process whose cwd happens to mention comfy must still be
        labeled vllm — inference runtimes are checked first."""
        assert infer_runtime("python", "python -m vllm.entrypoints", "/data/comfy") == "vllm"


class TestCwd:
    def test_reads_cwd(self):
        class P:
            def cwd(self):
                return "/app/ComfyUI"

        assert _cwd(P()) == "/app/ComfyUI"

    def test_denied_cwd_is_empty_not_fatal(self):
        class P:
            def cwd(self):
                raise PermissionError("denied")

        assert _cwd(P()) == ""

    def test_missing_attribute(self):
        assert _cwd(object()) == ""


class TestCommandLine:
    """Falls back across nvitop's two forms, and tolerates both being denied —
    /proc/<pid>/cmdline can be unreadable when the agent runs as non-root.
    """

    def test_prefers_structured_cmdline(self):
        class P:
            def cmdline(self):
                return ["python", "-m", "vllm.entrypoints.openai.api_server"]

            def command(self):
                return "should not be used"

        assert _command_line(P()) == "python -m vllm.entrypoints.openai.api_server"

    def test_falls_back_to_command_string(self):
        class P:
            def cmdline(self):
                raise PermissionError("denied")

            def command(self):
                return "python -m vllm"

        assert _command_line(P()) == "python -m vllm"

    def test_empty_cmdline_falls_through(self):
        class P:
            def cmdline(self):
                return []

            def command(self):
                return "python -m vllm"

        assert _command_line(P()) == "python -m vllm"

    def test_returns_empty_when_both_denied(self):
        """Must degrade to an unlabeled process, never raise — a process can
        exit mid-scan or belong to another user."""

        class P:
            def cmdline(self):
                raise PermissionError("denied")

            def command(self):
                raise PermissionError("denied")

        assert _command_line(P()) == ""

    def test_handles_missing_attributes(self):
        assert _command_line(object()) == ""


class TestNumConversion:
    def test_converts_numbers(self):
        assert _num(42) == 42.0
        assert _num("3.5") == 3.5

    def test_na_sentinel_becomes_none(self):
        """nvitop returns an `NA` sentinel rather than raising, and it's falsy —
        treating it as a number would report 0 for missing telemetry."""
        from nvitop import NA

        assert _num(NA) is None

    def test_none_stays_none(self):
        assert _num(None) is None

    def test_garbage_becomes_none(self):
        assert _num("not-a-number") is None
        assert _num(object()) is None
