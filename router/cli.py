"""`router probe`: ask every vendor what it has left and write it into the ledger.

This is the one command an operator runs to make the router *quota-aware* instead of
reactive: it fills `remaining` from the vendor's own accounting API, so the next search
already knows whether the free pool is alive. It is also what a cron job should call —

    17 */6 * * *  /root/.hermes/venv/bin/python -m router.cli probe --quiet

Run it before a release too: a vendor that changes its usage endpoint shows up here.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import config, ledger as ledger_mod
from .provider import get_router


def _fmt_int(value: float) -> str:
    return f"{value:,.0f}"


def cmd_status(args: argparse.Namespace) -> int:
    settings = config.settings(refresh=True)
    router = get_router()
    rows = []
    now = router.now()
    for name in dict.fromkeys(list(settings.search_order) + list(settings.extract_order)):
        state = router.ledger.get(name)
        stage = router.stages.get(name)
        unit = stage.unit if stage else "?"
        left = "unmetered" if state.limit <= 0 else _fmt_int(state.remaining())
        limit = "-" if state.limit <= 0 else _fmt_int(state.limit)
        window = "-" if state.reset_at is None else f"to {_iso(state.reset_at)}"
        rows.append({
            "vendor": name,
            "credential": "yes" if stage and stage.has_credential() else "no",
            "left": left,
            "limit": limit,
            "unit": unit,
            "window": window,
            "paused": _pause(state, now),
            "source": state.source,
            "search": bool(stage and stage.capability.search),
            "extract": bool(stage and stage.capability.extract),
        })

    if args.json:
        print(json.dumps({
            "state_dir": str(settings.state_dir),
            "search_order": list(settings.search_order),
            "extract_order": list(settings.extract_order),
            "vendors": rows,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"state: {settings.state_dir}")
    print(f"search order : {', '.join(settings.search_order)}")
    print(f"extract order: {', '.join(settings.extract_order)}")
    print()
    header = f"{'vendor':10} {'cred':4} {'left':>12} {'limit':>9} {'unit':8} {'window':22} {'paused':22} {'src':6}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['vendor']:10} {row['credential']:4} {row['left']:>12} {row['limit']:>9} "
              f"{row['unit']:8} {row['window']:22} {row['paused']:22} {row['source']:6}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    router = get_router()
    results = router.probe_all(force=True)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for name, info in results.items():
        left = info.get("remaining")
        if left is None:
            print(f"{name:10} probe failed ({info.get('error') or 'no answer'}) — ledger unchanged")
        else:
            print(f"{name:10} {_fmt_int(float(left))} {info.get('unit', '')} left "
                  f"(limit {_fmt_int(float(info.get('limit') or 0))}, source {info.get('source')})")
    return 0 if all(v.get("remaining") is not None for v in results.values()) else 1


def cmd_route(args: argparse.Namespace) -> int:
    router = get_router()
    for capability in ("search", "extract"):
        print(f"{capability}:")
        usable, skipped = router.candidates(capability)
        for index, stage in enumerate(usable):
            print(f"  {'usable' if index == 0 else 'next  '}: {stage.name}")
        if not usable:
            print("  unusable: nothing can serve this call")
        for vendor, reason in router.skips(capability):
            print(f"  skip {vendor}: {reason}")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    ledger = ledger_mod.Ledger(config.settings(refresh=True).ledger_path)
    ledger.disable(args.vendor, args.reason or "disabled by operator")
    print(f"{args.vendor}: disabled ({args.reason or 'no reason given'})")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    ledger = ledger_mod.Ledger(config.settings(refresh=True).ledger_path)
    ledger.enable(args.vendor)
    print(f"{args.vendor}: enabled")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe the local accounting for a vendor (use after a plan change or a vendor reset)."""
    ledger = ledger_mod.Ledger(config.settings(refresh=True).ledger_path)
    ledger.reset(args.vendor)
    print(f"{args.vendor}: local accounting cleared")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the chain works right now, against the vendors that actually have credentials."""
    router = get_router()
    ok = True
    result = router.run_search(args.query, 3)
    served = f" ({result.stage})" if result.stage else ""
    print(f"search '{args.query}': {'ok' if result.ok else 'FAILED'}{served}")
    print(f"  chain: {result.chain_text()}")
    ok &= result.ok
    if result.ok:
        for row in (result.payload or {}).get("data", {}).get("web", [])[:3]:
            print(f"  - {row.get('title')} {row.get('url')}")
    url = args.url
    extract = router.run_extract([url])
    entry = (extract.payload or [{}])[0]
    good = bool(entry.get("content"))
    print(f"extract {url}: {'ok' if good else 'FAILED'} "
          f"({len(entry.get('content') or '')} chars)")
    print(f"  chain: {extract.chain_text()}")
    ok &= good
    return 0 if ok else 1


def _pause(state: ledger_mod.VendorState, now: float) -> str:
    if not state.is_paused(now):
        return "-"
    left = state.pause_left(now)
    if left == float("inf"):
        return state.pause_kind
    hours = left / 3600.0
    return f"{state.pause_kind} {hours:.1f}h" if hours >= 1 else f"{state.pause_kind} {int(left // 60)}m"


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    import datetime as dt

    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router",
        description="Quota-aware web search/extract router for Hermes Agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show what is left in every vendor's free pool")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    probe = sub.add_parser("probe", help="ask vendors for their real remaining quota and store it")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=cmd_probe)

    route = sub.add_parser("route", help="show which vendor a call would use now, and why not others")
    route.set_defaults(func=cmd_route)

    disable = sub.add_parser("disable", help="park a vendor by hand")
    disable.add_argument("vendor")
    disable.add_argument("--reason", default="")
    disable.set_defaults(func=cmd_disable)

    enable = sub.add_parser("enable", help="un-park a vendor by hand")
    enable.add_argument("vendor")
    enable.set_defaults(func=cmd_enable)

    reset = sub.add_parser("reset", help="clear the local accounting for a vendor")
    reset.add_argument("vendor")
    reset.set_defaults(func=cmd_reset)

    selftest = sub.add_parser("selftest", help="run one search and one extract end-to-end")
    selftest.add_argument("--query", default="Nous Research Hermes Agent")
    selftest.add_argument("--url", default="https://example.com/")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
