"""
Unit tests for the agent-CLI adapter (backend/services/llm.py).

Every AI feature funnels through here, so these cover the two things that
differ between the Codex and Claude backends: how the argv is built, and where
the final message is read from.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from config import settings
from services import llm


def _codex_ok(reply: str):
    """Fake a successful `codex exec`: write `reply` to the -o path it was given."""
    def side_effect(argv, **kwargs):
        out_path = Path(argv[argv.index("-o") + 1])
        out_path.write_text(reply)
        return subprocess.CompletedProcess(argv, 0, stdout="banner noise\n", stderr="")
    return side_effect


def _codex_fail(returncode: int = 1, stderr: str = "boom"):
    def side_effect(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout="banner", stderr=stderr)
    return side_effect


@pytest.fixture
def codex_backend(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "codex")
    monkeypatch.setattr(settings, "llm_bin", "/usr/bin/codex")
    monkeypatch.setattr(settings, "llm_model", "")


@pytest.fixture
def claude_backend(monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "claude")
    monkeypatch.setattr(settings, "llm_bin", "/usr/bin/claude")


# ── Codex path ────────────────────────────────────────────────────────────────

class TestCodexRun:

    def test_reads_final_message_not_stdout(self, codex_backend):
        """stdout carries the banner and token count; only -o holds the answer."""
        with patch("subprocess.run", side_effect=_codex_ok("the answer")):
            assert llm.run("prompt") == "the answer"

    def test_argv_is_non_interactive_and_sandboxed(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("my prompt")
        argv = p.call_args[0][0]
        assert argv[:2] == ["/usr/bin/codex", "exec"]
        assert "--skip-git-repo-check" in argv
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in argv
        assert argv[-1] == "my prompt"

    def test_stdin_is_closed(self, codex_backend):
        """Otherwise Codex appends the inherited pipe as a <stdin> block."""
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("prompt")
        assert p.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_runs_in_a_scratch_workdir(self, codex_backend):
        """The workspace root must not be the app checkout — no source, no AGENTS.md."""
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("prompt")
        argv = p.call_args[0][0]
        workdir = Path(argv[argv.index("-C") + 1])
        assert workdir.name.startswith("distillpod-llm-")
        assert not (workdir / "backend").exists()

    def test_scratch_workdir_is_cleaned_up(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("prompt")
        argv = p.call_args[0][0]
        assert not Path(argv[argv.index("-C") + 1]).exists()

    def test_model_passed_when_configured(self, codex_backend, monkeypatch):
        monkeypatch.setattr(settings, "llm_model", "gpt-5.6-terra")
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("prompt")
        argv = p.call_args[0][0]
        assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"

    def test_no_model_flag_by_default(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("ok")) as p:
            llm.run("prompt")
        assert "-m" not in p.call_args[0][0]

    def test_nonzero_exit_raises(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_fail(2, "auth expired")):
            with pytest.raises(llm.LLMError, match="auth expired"):
                llm.run("prompt")

    def test_timeout_raises(self, codex_backend):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 60)):
            with pytest.raises(llm.LLMError, match="timed out"):
                llm.run("prompt", timeout=60)

    def test_missing_output_file_raises(self, codex_backend):
        """Exit 0 but nothing written — treat as failure, not as empty success."""
        with patch("subprocess.run", side_effect=_codex_fail(0)):
            with pytest.raises(llm.LLMError, match="no final message"):
                llm.run("prompt")

    def test_empty_reply_raises(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("   \n  ")):
            with pytest.raises(llm.LLMError, match="empty"):
                llm.run("prompt")

    def test_default_swallows_failure(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_fail()):
            assert llm.run("prompt", default="") == ""


class TestCodexSchema:

    SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}

    def test_schema_written_and_passed(self, codex_backend):
        seen = {}

        def side_effect(argv, **kwargs):
            path = Path(argv[argv.index("--output-schema") + 1])
            seen["schema"] = json.loads(path.read_text())
            Path(argv[argv.index("-o") + 1]).write_text('{"a": "b"}')
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            assert llm.run_json("prompt", schema=self.SCHEMA) == {"a": "b"}
        assert seen["schema"] == self.SCHEMA

    def test_no_schema_flag_when_unconstrained(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok('{"a": "b"}')) as p:
            llm.run_json("prompt")
        assert "--output-schema" not in p.call_args[0][0]

    def test_fences_tolerated_despite_schema(self, codex_backend):
        """A schema should prevent fences; strip them anyway rather than fail."""
        with patch("subprocess.run", side_effect=_codex_ok('```json\n{"a": "b"}\n```')):
            assert llm.run_json("prompt", schema=self.SCHEMA) == {"a": "b"}

    def test_unparseable_json_raises(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("sorry, I can't help")):
            with pytest.raises(llm.LLMError, match="expected JSON"):
                llm.run_json("prompt", schema=self.SCHEMA)

    def test_unparseable_json_returns_default(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("not json")):
            assert llm.run_json("prompt", default={"ads": []}) == {"ads": []}


# ── Claude fallback path ──────────────────────────────────────────────────────

class TestClaudeBackend:

    def test_reads_stdout(self, claude_backend):
        done = subprocess.CompletedProcess([], 0, stdout="the answer\n", stderr="")
        with patch("subprocess.run", return_value=done) as p:
            assert llm.run("prompt") == "the answer"
        assert p.call_args[0][0] == ["/usr/bin/claude", "--print", "prompt"]

    def test_strips_fences(self, claude_backend):
        """Claude has no --output-schema, so JSON comes back fenced."""
        done = subprocess.CompletedProcess([], 0, stdout='```json\n{"a": 1}\n```', stderr="")
        with patch("subprocess.run", return_value=done):
            assert llm.run_json("prompt") == {"a": 1}

    def test_nonzero_exit_raises(self, claude_backend):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="not logged in")
        with patch("subprocess.run", return_value=done):
            with pytest.raises(llm.LLMError, match="not logged in"):
                llm.run("prompt")


# ── Async wrappers ────────────────────────────────────────────────────────────

class TestAsyncWrappers:

    @pytest.mark.asyncio
    async def test_arun(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_ok("hello")):
            assert await llm.arun("prompt") == "hello"

    @pytest.mark.asyncio
    async def test_arun_json_default_on_failure(self, codex_backend):
        with patch("subprocess.run", side_effect=_codex_fail()):
            assert await llm.arun_json("prompt", default=None) is None


# ── Binary resolution ─────────────────────────────────────────────────────────

class TestBinaryResolution:

    def test_explicit_bin_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_backend", "codex")
        monkeypatch.setattr(settings, "llm_bin", "/opt/custom/codex")
        assert settings.llm == "/opt/custom/codex"

    def test_falls_back_to_path_lookup(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_backend", "claude")
        monkeypatch.setattr(settings, "llm_bin", "")
        with patch("shutil.which", return_value="/usr/local/bin/claude") as which:
            assert settings.llm == "/usr/local/bin/claude"
        which.assert_called_once_with("claude")

    def test_bare_name_when_not_on_path(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_backend", "codex")
        monkeypatch.setattr(settings, "llm_bin", "")
        with patch("shutil.which", return_value=None):
            assert settings.llm == "codex"


class TestObjectShapeGuard:
    """The Claude backend has no --output-schema, so a bare array can come back
    where an object was asked for. Callers do .get() on the result, so reject it."""

    OBJ_SCHEMA = {"type": "object", "properties": {"topics": {"type": "array"}}}

    def test_array_where_object_expected_raises(self, claude_backend):
        done = subprocess.CompletedProcess([], 0, stdout='["a", "b"]', stderr="")
        with patch("subprocess.run", return_value=done):
            with pytest.raises(llm.LLMError, match="expected a JSON object"):
                llm.run_json("prompt", schema=self.OBJ_SCHEMA)

    def test_array_where_object_expected_returns_default(self, claude_backend):
        done = subprocess.CompletedProcess([], 0, stdout='["a", "b"]', stderr="")
        with patch("subprocess.run", return_value=done):
            assert llm.run_json("prompt", schema=self.OBJ_SCHEMA, default={}) == {}

    def test_object_passes(self, claude_backend):
        done = subprocess.CompletedProcess([], 0, stdout='{"topics": ["a"]}', stderr="")
        with patch("subprocess.run", return_value=done):
            assert llm.run_json("prompt", schema=self.OBJ_SCHEMA) == {"topics": ["a"]}

    def test_unschemad_array_still_allowed(self, claude_backend):
        done = subprocess.CompletedProcess([], 0, stdout='["a"]', stderr="")
        with patch("subprocess.run", return_value=done):
            assert llm.run_json("prompt") == ["a"]
