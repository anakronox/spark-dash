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
    infer_model,
    infer_runtime,
)

# Verbatim from the GX10 on 2026-08-15, trimmed only in length. The child that
# holds the weights (26.4 GiB) carries --alias; the router parent that spawned
# it carries --models-preset and holds ~0.17 GiB of its own.
GX10_MODEL_CHILD = (
    "/app/llama-server --cache-prompt --cache-reuse 2048 --host 127.0.0.1 "
    "--metrics --port 52447 --sleep-idle-seconds 1200 --slots --spec-draft-n-max 3 "
    "--spec-type draft-mtp --alias qwen36-35b --batch-size 4096 --ctx-size 131072 "
    "--cont-batching --cache-type-k q8_0 --flash-attn on --load-mode none "
    "--model /models/Qwen3.6-Heretic-NVFP4-MTP/Qwen3.6-35B-A3B-uncen"
)
GX10_ROUTER_PARENT = (
    "/app/llama-server --host 0.0.0.0 --port 8000 --models-preset /config/models.ini "
    "--models-max 3 --models-autoload --metrics"
)


class TestInferModel:
    def test_alias_from_a_real_child_command(self):
        assert infer_model(GX10_MODEL_CHILD) == "qwen36-35b"

    def test_router_parent_has_no_model(self):
        """The parent serves every model and holds none of their weights.
        Attributing its memory to a model would be a lie."""
        assert infer_model(GX10_ROUTER_PARENT) is None

    def test_equals_form(self):
        assert infer_model("llama-server --alias=qwen36-35b --port 1") == "qwen36-35b"

    def test_model_path_is_not_mistaken_for_the_alias(self):
        """--model names a file; --alias names the thing the router reports.
        Only the latter joins to the router metrics."""
        assert infer_model("llama-server --model /models/Qwen3.6-35B.gguf") is None

    def test_trailing_alias_with_no_value(self):
        """Malformed rather than meaningful — must not swallow the next flag."""
        assert infer_model("llama-server --port 8000 --alias") is None
        assert infer_model("llama-server --alias --metrics") is None

    def test_empty_equals_form(self):
        assert infer_model("llama-server --alias=") is None

    def test_no_command_line(self):
        assert infer_model("") is None

    def test_non_llm_process(self):
        assert infer_model("python main.py --listen 0.0.0.0 --port 8188") is None


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


class FakeSample:
    def __init__(self, pid, sm, enc=0, dec=0):
        self.pid, self.smUtil, self.encUtil, self.decUtil = pid, sm, enc, dec


class FakeDevice:
    handle = object()


class TestProcessUtilization:
    """Per-process compute, which memory cannot show.

    Measured on the GX10 with real contention: ComfyUI at 75-91% SM against
    llama-servers that were merely resident. Plotting bytes alone made those
    look identical.
    """

    def test_maps_pids_to_sm_encoder_decoder(self, monkeypatch):
        import spark_dash_agent.collectors.gpu as gpu

        monkeypatch.setattr(
            gpu.libnvml,
            "nvmlDeviceGetProcessUtilization",
            lambda handle, since: [FakeSample(2553559, 90), FakeSample(3339966, 3, enc=74, dec=12)],
            raising=False,
        )
        util = gpu.read_process_utilization(FakeDevice())
        assert util[2553559] == (90.0, 0.0, 0.0)
        assert util[3339966] == (3.0, 74.0, 12.0)

    def test_no_samples_is_empty_not_an_error(self, monkeypatch):
        """NVML raises NotFound when nothing has been active in the window,
        which is an ordinary idle GPU rather than a failure."""
        import spark_dash_agent.collectors.gpu as gpu

        def boom(handle, since):
            raise RuntimeError("NVML_ERROR_NOT_FOUND")

        monkeypatch.setattr(
            gpu.libnvml, "nvmlDeviceGetProcessUtilization", boom, raising=False
        )
        assert gpu.read_process_utilization(FakeDevice()) == {}

    def test_unsupported_api_is_not_fatal(self, monkeypatch):
        import spark_dash_agent.collectors.gpu as gpu

        monkeypatch.delattr(gpu.libnvml, "nvmlDeviceGetProcessUtilization", raising=False)
        assert gpu.read_process_utilization(FakeDevice()) == {}
