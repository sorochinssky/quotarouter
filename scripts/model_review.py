"""Ask several unrelated models to review this package, without spending money on it.

Run from the repository root:

    python3 scripts/model_review.py                 # every reviewer
    python3 scripts/model_review.py nemotron ernie  # just these

Free tiers only: OpenRouter free models get the source pasted in, opencode's free models read the
files themselves. Answers land as JSON under `reviews/` (git-ignored) — one file per model, so a
finding can be traced back to whoever made it. Nothing here judges the findings: a model's claim
is a hypothesis until someone reads the cited line.

Three transports, because the models live in different places:

* OpenRouter free models (NVIDIA Nemotron 550B, Baidu Ernie) get the source pasted in — they
  have no tools and no filesystem.
* opencode CLI free models (GLM, LongCat/Nemotron hosted by opencode) get the repository path
  and read the files themselves, which keeps the prompt small and lets them grep.

Every reviewer must answer with JSON only, so the findings can be compared mechanically. The
script does not judge the findings — it stores raw answers; verification happens afterwards by
reading the code.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

REPO = Path(os.environ.get("QUOTAROUTER_REPO") or Path.cwd()).resolve()
OUT = Path(os.environ.get("QUOTAROUTER_REVIEW_DIR") or REPO / "reviews")
OUT.mkdir(parents=True, exist_ok=True)


def _env() -> dict[str, str]:
    """Keys from the process environment, plus an optional dotenv file."""
    values = dict(os.environ)
    dotenv = Path(os.environ.get("QUOTAROUTER_ENV_FILE") or Path.home() / ".hermes" / ".env")
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                name, value = line.split("=", 1)
                values.setdefault(name.strip(), value.strip())
    return values


ENV = _env()

PACKAGE_FILES = sorted(str(p.relative_to(REPO)) for p in (REPO / "quotarouter").rglob("*.py"))
CORE_FILES = [
    "quotarouter/routing.py",
    "quotarouter/ledger.py",
    "quotarouter/errors.py",
    "quotarouter/types.py",
    "quotarouter/config.py",
    "quotarouter/retention.py",
    "quotarouter/alerts.py",
    "quotarouter/stages/base.py",
    "quotarouter/stages/jina.py",
    "quotarouter/stages/firecrawl.py",
]

INSTRUCTIONS = """You are a senior Python reviewer with production on-call experience. Review the
source below of `quotarouter`: a library that routes web-search and web-extract calls over
several paid vendor APIs, tracks their remaining quota in a ledger, parks exhausted or broken
vendors, records an audit trail, sends alerts on degradation, and is exposed both as a Hermes
plugin and as an MCP server (line-delimited JSON-RPC 2.0 over stdio).

Hunt for defects that would actually bite in production, in this order of importance:

1. Silent wrongness — a call is reported successful while the caller gets no data; a page or a
   result is dropped without an error; the ledger says a vendor has quota when it does not.
2. Crashes — unhandled exceptions on the call path, attribute errors on odd vendor payloads,
   None arithmetic, wrong exception types in `except` clauses.
3. Concurrency — the router is used from a threaded gateway and now fetches several URLs in a
   thread pool; look for races in ledger/alerts/retention file writes, shared mutable state,
   lost updates, partial files.
4. Security — a secret (API key, token) reaching a log line, an error message returned to the
   caller, the audit trail or an exception traceback; shell injection through configuration.
5. Accounting — off-by-one in remaining quota, window reset at the wrong moment, a pause that
   never lifts or lifts too early, double counting on retry.
6. Contracts — MCP/JSON-RPC framing errors, a tool argument that is accepted but ignored, a
   response shape the caller cannot parse.

Rules:
- Cite every finding as `file:line` exactly as numbered in the source. A finding without a line
  is not a finding.
- No style, naming or docstring nits. No "consider adding type hints". No invented APIs: if you
  claim `httpx` or `json` behaves a certain way, be sure.
- If you suspect something but cannot prove it from the code, include it with "certain": false.
- Prefer one precise finding over five vague ones. Ten real findings beat forty.

Answer with JSON only, no prose around it:

{"verdict": "approve" | "changes" | "reject",
 "summary": "<= 400 chars",
 "findings": [{"severity": "critical" | "major" | "minor",
               "file": "...", "line": 123,
               "title": "short imperative",
               "why": "the failure mode, concretely",
               "fix": "what to change",
               "certain": true}]}
"""


def bundle(files: list[str]) -> str:
    parts = []
    for rel in files:
        text = (REPO / rel).read_text(encoding="utf-8")
        numbered = "\n".join(f"{i:5d}| {line}" for i, line in enumerate(text.splitlines(), 1))
        parts.append(f"===== {rel} =====\n{numbered}\n")
    return "\n".join(parts)


BACKSLASH = chr(92)


def unescape(text: str) -> str:
    """Undo one layer of backslash escaping.

    A model that prints JSON inside a JSON string field hands back `{\"verdict\": ...}`, which
    parses as nothing. That cost a full set of five findings once: it was recorded as
    "unparsed" and the run was written off as a model that could not follow instructions.
    """
    out: list[str] = []
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char == BACKSLASH and index + 1 < size:
            following = text[index + 1]
            mapped = {'"': '"', BACKSLASH: BACKSLASH, "n": chr(10), "t": chr(9), "/": "/"}.get(following)
            if mapped is not None:
                out.append(mapped)
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def salvage_findings(text: str) -> tuple[list[dict], str]:
    """Take as many complete findings as the answer contains, even when it was cut off.

    Reasoning models start the `findings` array and then run out of tokens mid-object. The
    object before the cut is still valid and still evidence; discarding it because the
    closing bracket is missing throws away the whole review.
    """
    decoder = json.JSONDecoder()
    verdict = "changes"
    head = re.search(r'"verdict"\s*:\s*"([a-z]+)"', text)
    if head:
        verdict = head.group(1)
    marker = text.find('"findings"')
    if marker < 0:
        return [], verdict
    start = text.find("[", marker)
    if start < 0:
        return [], verdict
    items: list[dict] = []
    position = start + 1
    while True:
        while position < len(text) and text[position] in " \n\r\t,":
            position += 1
        if position >= len(text) or text[position] != "{":
            break
        try:
            item, position = decoder.raw_decode(text, position)
        except ValueError:
            break
        if isinstance(item, dict):
            items.append(item)
    return items, verdict


def extract_json(raw: str) -> dict:
    """Models wrap JSON in fences or prose; take the outermost object that parses."""
    raw = raw or ""
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S)
    for candidate in fenced + [raw, unescape(raw)]:
        candidate = candidate.strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if isinstance(parsed, dict) and "findings" in parsed:
            return parsed
    for candidate in (raw, unescape(raw)):
        items, verdict = salvage_findings(candidate)
        if items:
            return {"verdict": verdict, "summary": "(answer was cut off; salvaged what parsed)",
                    "findings": items, "raw_tail": raw[-800:]}
    return {"verdict": "unparsed", "summary": raw[:400], "findings": [], "raw_tail": raw[-800:]}


def fingerprint(files: list[str]) -> str:
    """Record which code state the reviewer saw, so a stale answer can be spotted later."""
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode("utf-8"))
        digest.update((REPO / rel).read_bytes())
    return digest.hexdigest()[:12]


def ask_openrouter(name: str, model: str, files: list[str], timeout: float = 900.0) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": bundle(files)},
        ],
        "max_tokens": 16000,
    }
    last = ""
    for attempt in range(3):
        try:
            response = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {ENV['OPENROUTER_API_KEY']}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            if response.status_code == 200:
                body = response.json()
                message = body["choices"][0]["message"] or {}
                # Reasoning models sometimes leave `content` empty and put everything in
                # `reasoning`; a None here used to crash the parser instead of the reviewer.
                raw = message.get("content") or message.get("reasoning") or ""
                parsed = extract_json(raw)
                finish = (body["choices"][0].get("finish_reason") or "")
                parsed["_model"] = body.get("model") or model
                parsed["_transport"] = "openrouter"
                parsed["_truncated"] = finish == "length"
                parsed["_source_sha"] = fingerprint(files)
                parsed["_raw"] = raw
                return parsed
            last = f"HTTP {response.status_code}: {response.text[:300]}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        import time
        time.sleep(5 * (attempt + 1))
    return {"verdict": "error", "summary": last, "findings": [], "_model": model,
            "_transport": "openrouter"}


def ask_opencode(name: str, model: str, timeout: float = 300.0) -> dict:
    prompt = (
        f"{INSTRUCTIONS}\n\nThe repository is at {REPO}. Read the files under quotarouter/ "
        "yourself (they are {}.py modules) and review them. Do NOT modify, create or delete any "
        "file: this is a read-only review. Print the JSON object as your final message."
    )
    try:
        done = subprocess.run(
            ["opencode", "run", "-m", model, prompt],
            cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
            # Without this the CLI waits for stdin to close, which under a supervisor that
            # hands it an open pipe never happens: the reviewer hangs until someone kills it,
            # and the run is then blamed on the model. Four "hanging" models were this.
            stdin=subprocess.DEVNULL,
        )
        raw = done.stdout
        parsed = extract_json(raw)
        parsed["_model"] = model
        parsed["_transport"] = "opencode"
        parsed["_source_sha"] = fingerprint(PACKAGE_FILES)
        parsed["_raw"] = raw
        if done.returncode != 0:
            parsed.setdefault("summary", "")
            parsed["summary"] = (parsed["summary"] + f" [exit {done.returncode}]")[:400]
        return parsed
    except Exception as exc:
        return {"verdict": "error", "summary": f"{type(exc).__name__}: {exc}", "findings": [],
                "_model": model, "_transport": "opencode"}


REVIEWERS = [
    ("nemotron", lambda: ask_openrouter("nemotron", "nvidia/nemotron-3-ultra-550b-a55b:free", PACKAGE_FILES)),
    ("ernie", lambda: ask_openrouter("ernie", "baidu/ernie-5.5-flash-preview:free", CORE_FILES)),
    ("dots", lambda: ask_openrouter("dots", "dots-studio/dots-3-note-preview:free", CORE_FILES)),
    ("particle", lambda: ask_openrouter("particle", "particle-ai/particle-1:free", CORE_FILES)),
    ("flint", lambda: ask_openrouter("flint", "recursive/flint-v1.0:free", CORE_FILES)),
    ("glm-opencode", lambda: ask_opencode("glm-opencode", "opencode/glm-5.3-flash-free")),
    ("nemotron-opencode", lambda: ask_opencode("nemotron-opencode", "opencode/nemotron-3.5-lightning-free")),
    ("longcat", lambda: ask_opencode("longcat", "opencode/longcat-2.5-free")),
    ("mimo", lambda: ask_opencode("mimo", "opencode/mimo-v2.6-flash-free")),
]

# Which vendors are behind each reviewer, so a report can say "independent" honestly: two models
# from one vendor are one opinion (see docs/reviews/2026-11-22-model-review.md).
FAMILIES = {
    "nemotron": "nvidia", "ernie": "baidu", "dots": "dots-studio", "particle": "particle-ai",
    "flint": "recursive", "glm-opencode": "zhipu", "nemotron-opencode": "nvidia",
    "longcat": "meituan", "mimo": "xiaomi",
}


def main() -> int:
    only = set(sys.argv[1:])
    jobs = [(name, fn) for name, fn in REVIEWERS if not only or name in only]
    results = {}
    with cf.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs}
        for future in cf.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = {"verdict": "error", "summary": f"{type(exc).__name__}: {exc}",
                                 "findings": []}
            (OUT / f"{name}.json").write_text(
                json.dumps(results[name], ensure_ascii=False, indent=2), encoding="utf-8")
            count = len(results[name].get("findings") or [])
            notes = "".join(flag for flag in ("_truncated",) if results[name].get(flag))
            print(f"{name:20} [{FAMILIES.get(name, '?'):12}] {results[name].get('verdict'):9} "
                  f"findings={count:2} {results[name].get('_model','')} {notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
