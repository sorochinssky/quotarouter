"""`python -m quotarouter ...` — the CLI without needing the console script on `PATH`.

Every MCP client config in the README starts the server this way, so it has to exist: `python -m
quotarouter serve-mcp`. Without `__main__` that command fails with "cannot be directly executed".
"""

from .cli import main

if __name__ == "__main__":  # pragma: no cover - the module guard itself, not a behaviour
    raise SystemExit(main())
