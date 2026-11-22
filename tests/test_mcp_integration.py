"""MCP integration: a real subprocess speaking line-delimited JSON-RPC 2.0.

No network: `initialize`, `tools/list` and `quota_status` read only the ledger. A search or
extract call would cost money, so those are left to the live and e2e levels.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class McpClient:
    """Minimal client: one JSON object per line, answers matched by id."""

    def __init__(self, state_dir: Path) -> None:
        env = dict(os.environ)
        env["QUOTAROUTER_STATE_DIR"] = str(state_dir)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("EXA_API_KEY", None)
        env.pop("FIRECRAWL_API_KEY", None)
        env.pop("DEEPSEEK_API_KEY", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "quotarouter.cli", "serve-mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd=str(REPO_ROOT),
        )
        self._next_id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "the server closed stdout without answering"
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=10)


def test_server_answers_initialize_and_lists_its_tools(tmp_path):
    client = McpClient(tmp_path / "state")
    try:
        init = client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "pytest", "version": "0"}})
        assert init["jsonrpc"] == "2.0"
        assert init["result"]["serverInfo"]["name"] == "quotarouter"
        assert "tools" in init["result"]["capabilities"]

        client.notify("notifications/initialized")
        listing = client.send("tools/list")
        names = {tool["name"] for tool in listing["result"]["tools"]}
        assert names == {"web_search", "web_extract", "quota_status"}
        for tool in listing["result"]["tools"]:
            assert tool["inputSchema"]["type"] == "object"
    finally:
        client.close()


def test_quota_status_reports_the_empty_ledger_without_network(tmp_path):
    client = McpClient(tmp_path / "state")
    try:
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "pytest", "version": "0"}})
        answer = client.send("tools/call", {"name": "quota_status", "arguments": {}})
        assert answer["result"]["isError"] is False
        # the payload is JSON per vendor: parse it instead of matching prose
        vendors = json.loads(answer["result"]["content"][0]["text"])
        assert "exa" in vendors and "firecrawl" in vendors
        # keyed vendors must report no credential in this process; keyless stages (jina) do
        # not need one and legitimately report true
        for name in ("exa", "firecrawl", "deepseek"):
            assert vendors[name]["credential"] is False, f"{name} must not see a key here"
    finally:
        client.close()


def test_unknown_tool_is_an_error_not_a_crash(tmp_path):
    client = McpClient(tmp_path / "state")
    try:
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "pytest", "version": "0"}})
        answer = client.send("tools/call", {"name": "no_such_tool", "arguments": {}})
        assert "error" in answer or answer["result"].get("isError") is True
        # still alive afterwards
        assert client.send("tools/list")["result"]["tools"]
    finally:
        client.close()


def test_missing_argument_is_reported_per_call(tmp_path):
    client = McpClient(tmp_path / "state")
    try:
        client.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "pytest", "version": "0"}})
        answer = client.send("tools/call", {"name": "web_search", "arguments": {}})
        payload = answer.get("error") or answer["result"]
        assert "query" in json.dumps(payload).lower()
    finally:
        client.close()
