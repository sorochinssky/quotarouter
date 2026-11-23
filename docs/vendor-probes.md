# Vendor measurements, 2026-11-22

## What is verified, and what is only written

"Supported" is a claim about code. This table is about evidence — measured on 2026-11-22 with
the real keys in `~/.hermes/.env`, from this host.

- **exa** — search and extract both answered live (`POST /contents` → 172 chars, `costDollars
  0.001`). Remaining credit is a local estimate; Exa exposes no balance endpoint.
- **firecrawl** — search answered live (`POST /v2/search` → 200, 2 credits), extract answers
  live (955/1000 credits, balance read from the vendor).
- **jina** — `GET https://r.jina.ai/<url>` answered 200 with page text, no key needed. Extract
  only; it has no search endpoint.
- **keyless** — `mcp.exa.ai/mcp` and `search.parallel.ai/mcp` answered live after two fixes
  below: search 0.53 s, extract 215 chars. Anonymous tier, not metered.
- **deepseek** — the key is valid and the balance endpoint answers, but
  `total_balance: -0.06`, `is_available: false` → every search is a 402. Parked in the ledger;
  `quotarouter enable deepseek` after a top-up.
- **tavily** — **never verified.** No key. The endpoint is real (`POST /search` with a dummy
  key answers `401 Unauthorized: missing or invalid API key`, `GET /usage` likewise), but no
  successful response has ever been seen. The test fixture is hand-written
  (`https://hermes.example/`), so it proves the mapping, not the vendor.
- **keenable** — **never verified, and it is first in the default search order.** No key.
  `POST /v1/search` with a dummy key answers `401 Malformed API key`, so the route exists, but
  the success shape is from a hand-written fixture (`https://k.example/`).

Two defects found by asking this question rather than trusting the code:

1. The keyless tier sent `Accept: application/json` and got `406 Client must accept both
   application/json and text/event-stream` on **every** call — the last-resort tier had never
   worked. It also could not read a reply framed as server-sent events.
2. Its extract called `web_fetch_exa` with `{"url": ...}`; the tool requires `{"urls": [...]}`
   and answers `200` with `isError: true` and the complaint as content. That complaint was
   parsed as the page, so a documentation page would have been replaced by
   `MCP error -32602: Invalid arguments`.

Both are fixed, with tests that assert the accept header, the event-stream framing, the `urls`
argument and the `isError` path.


Captured with live calls on production keys (`~/.hermes/.env`), so the ledger's quota maths
and the per-stage unit costs rest on facts rather than on price lists.

## Exa (search)

```
POST https://api.exa.ai/search
{"query": "Nous Research Hermes Agent", "numResults": 1, "contents": {"highlights": true}}
-> costDollars: {"total": 0.007, "search": {"neural": 0.007}, "contents": {"highlights": 0.0}}
```

- Plan: $10/month of paid credit, not "1000 free requests".
- The price does not depend on the result count up to 10: $0.007 per neural search with
  highlights.
- Hence roughly 1 400 searches per month for $10. Highlights are free.
- There is no public endpoint for the remaining credit; `MONTHLY_CREDIT_USD = 10.0` in the
  ledger is counted locally, and the window resets on the 1st.

## Firecrawl (search + scrape)

```
GET https://api.firecrawl.dev/v2/team/credit-usage
-> {"success": true, "data": {"remainingCredits": 989, "planCredits": 1000,
     "billingPeriodStart": "2026-10-30T07:28:51.871Z",
     "billingPeriodEnd":   "2026-11-30T07:28:51.871Z"}}
```

- `POST /v2/search` → `creditsUsed: 2` (2 credits per search, up to 5 results).
- `POST /v2/scrape` → 1 credit per successful page; a failed page is not billed.
- Total: 1 000 credits = ~500 searches OR 1 000 pages; one shared pool.
- The window rolls on the **30th**, not the 1st. `probe_quota()` parses `billingPeriodEnd`
  into the period key `firecrawl:YYYY-MM-DD`, so the ledger opens a new window by itself.
- This is the only vendor with a remaining-quota API, hence the only one whose ledger entry
  reads `source: api` instead of `local`.

## Jina Reader (extract)

- `https://r.jina.ai/<url>` without a key: 20 requests/min per IP.
- With `JINA_API_KEY`: the key's own limit on a paid plan.
- Returns `Title:` / `URL Source:` / `Markdown Content:`; an empty page and a JS wall are
  recognised as a stub (`looks_like_stub`), after which the chain moves to the next vendor.

## DeepSeek (search + fetch)

```
GET https://api.deepseek.com/user/balance
-> {"is_available": false, "balance_infos": [{"total_balance": "-0.06", ...}]}
```

- $0.75 per 1 000 successful requests = $0.00075 per call (~9× cheaper than Exa).
- The balance is negative: every call returns 402 `Insufficient Balance`.
- Strategy: the stage stays in the list, but `probe_quota()` sees `is_available: false` and
  **parks the stage** instead of calling it. After a top-up, either `quotarouter enable
  deepseek` or the next `probe` brings it back.

## What this means for the order

- **search**: exa (a $10 pool, cheap at 1 400 calls) → firecrawl (shared pool, 2 credits per
  search) → deepseek (balance-paid). Keenable/Tavily are listed but skipped without keys.
- **extract**: jina (free, no key) → firecrawl (1 credit per page) → exa (more expensive).
  Jina first preserves the scarcest pool — Firecrawl credits are shared with search.

## From an MCP client

`quota_probe` (the MCP tool) runs the same probes as `quotarouter probe` and returns the report
as JSON; `quota_status` returns what the ledger believes. Use `quota_probe` when a balance was
changed outside the router (a plan upgrade, a top-up) and `quota_status` for the everyday
question. Probing costs no vendor credits.
