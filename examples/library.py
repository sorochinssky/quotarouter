"""Runnable example: one search and one extract through the core, with no host involved.

    python examples/library.py "model context protocol"

Keys are read from the environment or from a dotenv file (`QUOTAROUTER_ENV_FILE`, `./.env`,
`~/.config/quotarouter/.env`). With no keys at all the call still answers through the anonymous
tier, which is why this example is safe to run before anything is configured.

The two shapes worth remembering:

- `run_search` payload is the vendor envelope: `{"success": True, "data": {"web": [...]}}`.
- `run_extract` payload is a list with one entry per requested URL, in the requested order;
  a page that failed carries `error` instead of `content`.
"""

from __future__ import annotations

import sys

from quotarouter import Router, format_failure


def search(router: Router, query: str, limit: int = 3) -> int:
    result = router.run_search(query, limit)
    if not result.ok:
        print(format_failure(result, "search"))
        return 1
    rows = (result.payload.get("data") or {}).get("web") or []
    print(f"served by: {result.stage} ({len(rows)} rows)")
    for row in rows:
        print(f"- {row.get('title') or '(no title)'}\n  {row.get('url')}")
    return 0


def extract(router: Router, urls: list[str]) -> int:
    result = router.run_extract(urls)
    if not result.ok:
        print(format_failure(result, "extract"))
        return 1
    print(f"served by: {result.stage}")
    for page in result.payload or []:
        if page.get("error"):
            print(f"- {page.get('url')}: {page['error']}")
        else:
            body = page.get("content") or ""
            print(f"- {page.get('url')}: {len(body)} chars — {page.get('title') or '(no title)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    query = args[0] if args else "model context protocol"
    urls = args[1:] or ["https://example.com/"]
    router = Router()
    code = search(router, query)
    return extract(router, urls) or code


if __name__ == "__main__":
    raise SystemExit(main())
