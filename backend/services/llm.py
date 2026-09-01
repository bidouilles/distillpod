"""
LLM adapter — the single place where the CLI coding agent is invoked.

DistillPod originally shelled out to `claude --print <prompt>`, which writes the
assistant's answer to stdout and nothing else. Codex is noisier: `codex exec`
prints a banner, session id, reasoning trace and a token count around the final
message, so its stdout cannot be parsed directly. Two flags fix that:

  -o/--output-last-message FILE   write ONLY the final message to FILE
  --output-schema FILE            constrain the final message to a JSON Schema

The schema flag is why the callers no longer strip markdown fences: Codex
returns bare JSON when a schema is supplied.

Set `LLM_BACKEND=claude` to fall back to the original Claude CLI path.
"""
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from config import settings

DEFAULT_TIMEOUT = 120


class LLMError(RuntimeError):
    """The agent CLI failed, timed out, or returned nothing usable."""


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers. Only needed on the Claude path and as a
    safety net if a model ignores the schema."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _codex_argv(prompt: str, workdir: str, out_file: str, schema_file: str | None) -> list[str]:
    argv = [
        settings.llm,
        "exec",
        "--skip-git-repo-check",   # the server's cwd is not a git checkout
        "--sandbox", "read-only",  # these are pure text tasks; no tool use needed
        "--ephemeral",             # don't accumulate session files on the VPS
        "--color", "never",
        "-C", workdir,             # empty dir: keeps AGENTS.md / app source out of context
        "-o", out_file,
    ]
    if settings.llm_model:
        argv += ["-m", settings.llm_model]
    if schema_file:
        argv += ["--output-schema", schema_file]
    argv.append(prompt)
    return argv


def run(
    prompt: str,
    *,
    schema: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    default: Any = ...,
) -> str:
    """Blocking call to the agent CLI. Returns the final message as text.

    Call from async code via `arun`, never directly — it blocks the event loop.
    Raises LLMError on non-zero exit, timeout, or empty output, unless `default`
    is given, in which case that value is returned instead.
    """
    try:
        if settings.llm_backend == "claude":
            return _run_claude(prompt, timeout=timeout)
        return _run_codex(prompt, schema=schema, timeout=timeout)
    except LLMError:
        if default is not ...:
            return default
        raise


def _run_claude(prompt: str, *, timeout: int) -> str:
    try:
        result = subprocess.run(
            [settings.llm, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise LLMError(f"claude exited {result.returncode}: {result.stderr[:200]}")
    out = _strip_fences(result.stdout)
    if not out:
        raise LLMError("claude returned an empty response")
    return out


def _run_codex(prompt: str, *, schema: dict | None, timeout: int) -> str:
    # A fresh empty directory per call: Codex treats it as the workspace root, so
    # the model sees no project files and no AGENTS.md it might act on.
    with tempfile.TemporaryDirectory(prefix="distillpod-llm-") as tmp:
        tmpdir = Path(tmp)
        out_file = tmpdir / "last-message.txt"
        schema_file = None
        if schema is not None:
            schema_file = tmpdir / "schema.json"
            schema_file.write_text(json.dumps(schema))

        argv = _codex_argv(prompt, str(tmpdir), str(out_file), str(schema_file) if schema_file else None)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,  # else Codex appends our tty/pipe as a <stdin> block
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"codex timed out after {timeout}s") from exc

        if result.returncode != 0:
            # Codex reports real errors on stderr; stdout holds the banner.
            detail = (result.stderr or result.stdout).strip()[-300:]
            raise LLMError(f"codex exited {result.returncode}: {detail}")

        if not out_file.exists():
            raise LLMError("codex produced no final message")
        out = _strip_fences(out_file.read_text())
        if not out:
            raise LLMError("codex returned an empty response")
        return out


def run_json(
    prompt: str,
    *,
    schema: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    default: Any = ...,
) -> Any:
    """Like `run`, but parses the reply as JSON.

    Pass `schema` (a JSON Schema dict) so Codex is constrained to that exact
    shape. Pass `default` to get that value back instead of an exception when
    the call or the parse fails.
    """
    try:
        raw = run(prompt, schema=schema, timeout=timeout)
        parsed = json.loads(raw)
        # Codex honours the schema, but the Claude backend has no equivalent flag —
        # it can hand back a bare array where an object was asked for, which would
        # blow up on .get() in the caller. Reject it here instead.
        if schema is not None and schema.get("type") == "object" and not isinstance(parsed, dict):
            raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed
    except (LLMError, json.JSONDecodeError) as exc:
        if default is not ...:
            return default
        raise LLMError(f"expected JSON from the agent: {exc}") from exc


async def arun(
    prompt: str,
    *,
    schema: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    default: Any = ...,
) -> str:
    """Non-blocking `run` — the CLI call happens in a worker thread."""
    from services import jobs
    async with jobs.lane("llm", label="agent call"):
        return await asyncio.to_thread(run, prompt, schema=schema, timeout=timeout, default=default)


async def arun_json(
    prompt: str,
    *,
    schema: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    default: Any = ...,
) -> Any:
    """Non-blocking `run_json`."""
    from services import jobs
    async with jobs.lane("llm", label="agent call"):
        return await asyncio.to_thread(run_json, prompt, schema=schema, timeout=timeout, default=default)
