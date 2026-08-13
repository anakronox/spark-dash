"""Runtime inference and command-line resolution.

Motivated by a real finding on the GX10: vLLM processes showed up as bare
`python` with no runtime label, because matching on the process name alone
can't identify them.
"""

import pytest
from spark_dash_agent.collectors.gpu import _command_line, _num, infer_runtime


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
