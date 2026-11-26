"""Run the Claude Code CLI as a tool-less, one-shot JSON answerer.

Shared by the optional observers. The CLI runs from an empty temporary folder outside
the repository, with no tools, MCP servers, skills, settings or saved session, and
only the environment variables listed in CLAUDE_ENV.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from argbot.watch_positions import read_json

# Claude Code runs on your own Claude Code login. Only these variables reach it:
# an API key would bill API credits instead, and broker secrets never leave the bot.
CLAUDE_ENV = {
    "PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "COMSPEC", "PATHEXT", "CLAUDE_CONFIG_DIR",
}


def write_new(path, data) -> None:
    """Write a JSON file that must not exist yet."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def claude_executable():
    found = shutil.which("claude.exe") or shutil.which("claude")
    for fallback in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude"):
        if not found and fallback.is_file():
            found = str(fallback)
    return found


def claude_env() -> dict:
    return {k: v for k, v in os.environ.items() if k.upper() in CLAUDE_ENV}


def command(executable, schema, p, system, *, tools=(), allowed=()):
    """One non-interactive answer: no MCP servers, skills, settings or saved session.

    Without `tools` no tool exists. With them, only the `allowed` rules run; dontAsk
    denies everything else (recorded as a permission denial, never prompted)."""
    args = [str(executable), "-p", "--model", p["model"], "--effort", p["reasoning_effort"],
            "--tools", ",".join(tools)]
    if allowed:
        args += ["--allowedTools", *allowed]
    return args + ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                   "--permission-mode", "dontAsk", "--setting-sources", "", "--output-format", "json",
                   "--json-schema", json.dumps(schema, ensure_ascii=False), "--system-prompt", system]


def run_claude(args, prompt, folder, p, runner=subprocess.run):
    """Runs from an empty directory outside the repository: no project context is loaded."""
    with tempfile.TemporaryDirectory(prefix="argbot-observer-") as cwd, \
            (Path(folder) / "result.json").open("x", encoding="utf-8") as stdout, \
            (Path(folder) / "stderr.log").open("x", encoding="utf-8") as stderr:
        return runner(args, input=prompt, text=True, encoding="utf-8", cwd=cwd, stdout=stdout, stderr=stderr,
                      timeout=p["analysis_timeout_seconds"], env=claude_env(),
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def claude_result(path, p, *, allow_denied=()):
    """Validate the CLI's single JSON result; returns (structured answer, usage).

    A denied call of a tool in `allow_denied` is recorded in usage; any other
    denial is an unexpected action."""
    r = read_json(path)
    if r.get("type") != "result" or r.get("subtype") != "success" or r.get("is_error") is not False:
        raise RuntimeError("Model run reported failure")
    denials = r.get("permission_denials") or []
    if any(not isinstance(d, dict) or d.get("tool_name") not in allow_denied for d in denials):
        raise RuntimeError("Unexpected model tool or action")
    if p["model"] not in (r.get("modelUsage") or {}):
        raise RuntimeError("Answer not produced by the reviewed model")
    answer = r.get("structured_output")
    if not isinstance(answer, dict):
        raise ValueError("Invalid analysis response")
    usage = {k: (r.get("usage") or {}).get(k) for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    usage["total_cost_usd"] = r.get("total_cost_usd")
    if denials:
        usage["denied_reads"] = [str((d.get("tool_input") or {}).get("url", ""))[:300] for d in denials]
    return answer, usage
