# Model review, 2026-11-22

An outside review of `quotarouter` 0.2.0 by models that did not write it. Kept in the repository
because the findings say more about the code than a coverage number does, and because a rejected
finding is as useful as an accepted one — it records what was considered and why it stands.

## Who reviewed

| Reviewer | Family | Transport | How it saw the code |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | NVIDIA Nemotron (MoE, large) | OpenRouter | whole package pasted, 131 KB |
| `opencode/nemotron-3.5-lightning-free` | NVIDIA Nemotron (small) | opencode CLI | read the files itself, with tools |
| `baidu/ernie-5.5-flash-preview:free` | Baidu Ernie | OpenRouter | core files pasted (routing, ledger, errors, retention, alerts, config, base, jina, firecrawl) |

Attempted and unavailable on the day: Claude Code (OAuth session expired), z.ai GLM-5.3 (weekly
limit exhausted), experiential-labs Claude/GPT (payment verification required), OpenRouter
`qwen3.8-27b:free` (moved to paid), `google/gemma-4-31b-it:free` and `inclusionai/ling-3.2-flash:free`
(upstream 429), `opencode/glm-5.3-flash-free` (server error). Only free tiers were used; nothing
was spent.

All three answered in prose or with escaped JSON rather than the requested schema, so their
findings were extracted by hand and then **verified line by line in the code** before anything
was changed. A model's claim is a hypothesis, not an audit result.

## Accepted and fixed (commit `0c44d76`)

1. **Extract charged one unit per call regardless of page count** (Nemotron #4, Ernie
   independently). Firecrawl bills a credit per page, so a six-URL extract moved the counter by
   one and the ledger believed a vendor still had credits it had already spent. This is the one
   defect that attacks the product's core promise. Fixed: the charge follows the delivered pages,
   and pages the site itself refused stay free.
2. **A vendor refusal delivered per page was filed as `bad_page`** (Nemotron #6 alongside Ernie).
   The adapter turned every `StageError` into a per-page error, so a 402 on all pages paused the
   stage for 60 s instead of parking it until the window reset — and kept retrying a dead
   account. Fixed: page errors carry their kind; `quota`, `auth`, `throttle`, `transient` pause
   the stage with that kind, while a per-domain block and the site's own 403 still never do.
3. **`RouteResult.ok` was true for a list of per-page errors** (Ernie). A total failure read as
   success because the payload was not `None`. Fixed: a stage must have served something.
4. **The ledger docstring promised a cross-process lock that did not exist** (Nemotron #2, also
   found independently while verifying). The gateway plugin, `quotarouter probe` from a timer and
   the MCP server are separate processes; two of them writing lost one charge. Fixed: every
   mutation is a transaction — in-process lock, `flock`, re-read, write. The test fails with
   `used = 2.0` when the transaction is disabled and passes with `5.0`.
5. **`reset_transport()` wrote to a global nothing reads** (Nemotron #10). A mock installed by
   one test stayed in force, so a "live" run could silently answer from fixtures — the worst
   possible failure for a test suite that exists to prove the vendors work. Fixed and tested.
6. **Alert text was substituted unquoted into argv** (Nemotron-small #1). Vendor error bodies end
   up in alert messages, so `$(...)` or `;` in a body could become shell syntax when the
   configured command used `sh -c`. Fixed: substitution is quoted and the text also arrives as
   `QUOTAROUTER_ALERT_TEXT`.
7. **A vendor-reported balance above the plan size** (Nemotron #13). Capped at the plan, so a
   bonus cannot make the window look unspent.

## Rejected, with the reason

- **"Ledger `get()` creates state outside the lock"** (Nemotron #1) — false. The creation happens
  inside `with self._lock`, and the lock is an `RLock`.
- **"A quota pause never lifts when `reset_at` is None"** (Nemotron-small #2) — false.
  `is_paused()` compares against `paused_until`, which `_pause` sets to `now + 24 h` when the
  reset moment is unknown; the stage becomes a candidate again when the deadline passes. The
  `reset_at` branch only shortens a pause, it does not extend it.
- **"`httpx.Client` is not thread-safe"** (Nemotron-small #5) — false for request execution; the
  documented contract is that a `Client` may be shared across threads. The lazy creation in
  `client()` is not guarded, which can build two clients once at startup — harmless, and
  `set_transport` only runs in tests and at plugin install.
- **"Prune keeps timestamp-less lines forever"** (Nemotron-small #4) — true and deliberate:
  dropping a line whose timestamp cannot be parsed deletes exactly the malformed evidence an
  incident needs. Rotation bounds the file regardless.
- **"MCP should send a response to notifications"** (Nemotron #8) — false, JSON-RPC 2.0 forbids
  it. The model marked this `certain: false` itself.
- **"MCP web_search returns JSON text instead of structured output"** (Nemotron #9) — a design
  choice: MCP text content is the transport-agnostic shape, and the tool declares no output
  schema. Not carried.

## Accepted, not changed

- **`outcome.spent or unit_cost` treats an explicit `0.0` as "not reported"** (Nemotron-small #3).
  Real, but the only stage that reports `0.0` is the keyless tier, whose unit cost is also `0.0`.
  Distinguishing "reported zero" from "not reported" needs `Optional[float]` across every adapter
  for no present benefit.
- **The keyless tier is not metered** (Nemotron #5). Its unit cost is `0.0` because there is no
  account to meter; it is gated by position (last) and by `keyless_last_resort`, not by price, so
  the claim that the router "prefers it when others have little quota" does not hold — the chain
  order decides.

## What this says about the test suite

Every accepted finding above was reachable by a test that did not exist: none of the 128 tests
asserted how many units an extract charges, or what `ok` means for a batch of error entries.
Coverage would not have caught it either — the lines were executed, they just were not asserted.
The 11 tests added in `tests/test_accounting.py` and `tests/test_ops.py` exist so these particular
silences cannot come back.

## Second round: the harness was the defect

Five reviewers were re-asked on the same day. Four came back `unparsed, 0 findings`, which looks
like free models that cannot follow an instruction. Reading the raw answers instead of the verdict
showed three harness defects, all of which lose findings silently:

- **A JSON answer escaped inside a JSON string.** `{\"verdict\": ...}` parses as nothing. This
  reviewer did produce findings — one of them, "shell injection in `_send_command`", was real and
  had already been fixed in `0c44d76`; the rest were lost with the verdict. Answers are now
  unescaped once before being called unparsed.
- **An answer cut off by the token limit.** The findings that did come out were complete objects;
  the array was just missing its closing bracket, and the whole review was dropped. Complete items
  are now salvaged, and the result is flagged as truncated.
- **`finish_reason` was ignored.** An output-limited answer looked exactly like a complete one.

The one recovered finding was stale: it cited the old `_send_command` that substituted alert text
into argv. That is not evidence the fix is wrong, but it is evidence that a review is only
meaningful against a named code state — each answer now records a fingerprint of the files it saw.

Reviewers that stayed unusable, for reasons outside the harness: `opencode/glm-5.3-flash-free`
returns a server error, `opencode/longcat-2.5-free` and `opencode/mimo-v2.6-flash-free` never
finish (killed at the timeout), `baidu/ernie-5.5-flash-preview:free` answers in prose and never
reaches JSON, and `opencode/nemotron-3.5-lightning-free` now times out as well. No paid call was
made at any point.

Net result of the second round: **no new actionable finding**, three harness defects fixed, seven
tests added.
