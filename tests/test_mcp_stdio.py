"""The real stdio transport: a subprocess speaks JSON-RPC, exactly as another agent would.

In-process tests never exercise the wrapper that turns a pipe into a text stream, and that is the
code every external client goes through. It is also where a bad argument must not end the session:
a model that guessed a stage name has to be able to try again in the same conversation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(messages: list[dict], tmp_path: Path) -> list[dict]:
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    chains = tmp_path / "chains.toml"
    chains.write_text('[chains.one]\nsearch = ["exa"]\n', encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "quotarouter.cli", "serve-mcp"],
        cwd=ROOT, input=payload, capture_output=True, text=True, timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "QUOTAROUTER_STATE_DIR": str(tmp_path / "state"),
            "QUOTAROUTER_ENV_FILE": str(tmp_path / "absent.env"),
            "QUOTAROUTER_CHAINS_FILE": str(chains),
            "QUOTAROUTER_ALLOW_KEYLESS": "0",
        },
    )
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def test_the_wire_carries_the_whole_session(tmp_path):
    answers = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "web_search", "arguments": {"query": "q", "stages": ["nope"]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "quota_status", "arguments": {}}},
    ], tmp_path)

    by_id = {answer["id"]: answer for answer in answers}
    assert by_id[1]["result"]["serverInfo"]["name"] == "quotarouter"

    tools = {tool["name"]: tool for tool in by_id[2]["result"]["tools"]}
    search_schema = tools["web_search"]["inputSchema"]
    for name in ("stages", "chain", "budget", "max_cost_usd", "force", "fallback"):
        assert name in search_schema["properties"], name
        assert name not in search_schema["required"], "an old client must not have to know them"

    bad = by_id[3]["result"]
    assert bad["isError"] is True
    assert "nope" in bad["content"][0]["text"] and "exa" in bad["content"][0]["text"]

    status = json.loads(by_id[4]["result"]["content"][0]["text"])
    assert status["allow_chain_override"] is True
    assert status["chains"]["one"]["search"] == ["exa"]
    assert status["vendors"]["deepseek"]["funding"] == "balance"
