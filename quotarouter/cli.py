"""`quotarouter` CLI: status, quota probe, which stage would serve, and a live selftest.

This is the one command an operator runs to make the router *quota-aware* instead of
reactive: it fills `remaining` from the vendor's own accounting API, so the next search
already knows whether the free pool is alive. It is also what a cron job should call —

    17 */6 * * *  python3 -m quotarouter.cli probe --quiet

Run it before a release too: a vendor that changes its usage endpoint shows up here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import alerts, config, ledger as ledger_mod, retention
from . import routing
from .routing import shared_router as get_router


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
            "pause_origin": ledger_mod.pause_origin_of(state) or None,
            "retry_at": _iso(state.paused_until) if state.is_paused(now) else None,
            "source": state.source,
            "search": bool(stage and stage.capability.search),
            "extract": bool(stage and stage.capability.extract),
            "funding": getattr(stage, "funding", "free") if stage else "?",
        })

    chains = settings.chains
    if args.json:
        print(json.dumps({
            "state_dir": str(settings.state_dir),
            "search_order": list(settings.search_order),
            "extract_order": list(settings.extract_order),
            "chains": chains.as_dict(),
            "chains_source": chains.source,
            "chains_path": chains.path,
            "chains_problems": [problem.as_line() for problem in chains.problems],
            "allow_chain_override": settings.allow_chain_override,
            "vendors": rows,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"state: {settings.state_dir}")
    print(f"search order : {', '.join(settings.search_order)}")
    print(f"extract order: {', '.join(settings.extract_order)}")
    print(f"chains       : {_chains_line(chains)}")
    if chains.problems:
        for problem in chains.problems:
            print(f"  dropped {problem.as_line()}")
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


def cmd_recheck(args: argparse.Namespace) -> int:
    """Ask the parked vendors whether they can pay again, and let the machine-parked ones back.

    An operator stop is never lifted here: `recheck` answers "is the vendor healthy", not
    "do I still mean it".
    """
    router = get_router()
    result = router.recheck()
    returned = result["returned"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for name in returned:
        print(f"{name}: back in service (the account can pay again)")
    if not returned:
        paused = [name for name, info in result["probe"].items() if info.get("paused")]
        print("nothing came back" + (f" — still out: {', '.join(paused)}" if paused else ""))
    return 0


def _chains_line(chains) -> str:
    if not chains.specs:
        return f"none (from {chains.source}, {chains.path or 'no path'})"
    names = ", ".join(chains.names())
    return f"{names} (from {chains.source}, {chains.path})"


def cmd_chains(args: argparse.Namespace) -> int:
    """What the file actually gave — including the entries it dropped, and why.

    Without this, diagnosing a chain configuration is guesswork: the file looks right, the name is
    missing, and there is no command that says which of the two is the problem.
    """
    settings = config.settings(refresh=True)
    chains = settings.chains
    if args.json:
        print(json.dumps({
            "source": chains.source,
            "path": chains.path,
            "chains": chains.as_dict(),
            "problems": [problem.as_line() for problem in chains.problems],
            "allow_chain_override": settings.allow_chain_override,
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"source: {chains.source}")
    print(f"path  : {chains.path or '-'}")
    if not chains.specs:
        print("no named chains")
    for name, spec in chains.specs.items():
        caps = ", ".join(spec.capabilities()) or "nothing"
        budget = spec.budget or ("max $%g" % spec.max_cost_usd if spec.max_cost_usd is not None
                                 else "no budget")
        print(f"  {name}: {caps} | {budget} | {'strict' if spec.strict else 'falls back'}")
        if spec.description:
            print(f"      {spec.description}")
        for capability in ("search", "extract"):
            stages = spec.stages_for(capability)
            if stages:
                print(f"      {capability}: {' -> '.join(stages)}")
    for problem in chains.problems:
        print(f"  dropped {problem.as_line()}")
    print(f"per-call overrides: {'allowed' if settings.allow_chain_override else 'disabled'}")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    router = get_router()
    # `getattr` rather than attribute access: `route` is also called from code that builds its own
    # namespace (a test, another tool), and a missing flag must not be an AttributeError.
    stages = [part.strip() for part in (getattr(args, "stages", None) or "").split(",")
              if part.strip()] or None
    chain = getattr(args, "chain", None)
    fallback = bool(getattr(args, "fallback", False))
    budget = getattr(args, "budget", None)
    max_cost = getattr(args, "max_cost", None)
    failed = False
    for capability in ("search", "extract"):
        try:
            resolved = router.resolve(
                capability, stages=stages, chain=chain, fallback=fallback,
                budget=budget, max_cost_usd=max_cost)
        except ValueError as exc:
            print(f"{capability}: refused — {exc}")
            failed = True
            continue
        print(f"{capability}: {resolved.mode} "
              f"({' -> '.join(resolved.names) or 'nothing'})"
              + (f" then {' -> '.join(resolved.fallback_names)}" if resolved.fallback_names else "")
              + (f" [budget={resolved.budget}]" if resolved.budget else "")
              + (f" [max ${resolved.max_cost_usd:g}]" if resolved.max_cost_usd is not None else "")
              + ("" if resolved.allow_keyless else " [no anonymous tier]"))
        # A page count of 1 keeps the estimate honest for "what would one call cost".
        usable, skipped = router.candidates(capability, resolved)
        for index, stage in enumerate(usable):
            print(f"  {'usable' if index == 0 else 'next  '}: {stage.name}")
        if not usable:
            # The anonymous tier is not a candidate (it has no quota to spend), but it will serve
            # the call — saying "unusable" while the call succeeds is a lie the operator acts on.
            anonymous = (router.stages.get("keyless")
                         if resolved.allow_keyless and config.settings(refresh=True).keyless_last_resort
                         else None)
            print("  fallback: keyless (anonymous tier)" if anonymous is not None
                  else "  unusable: nothing can serve this call")
        for skip in skipped:
            print(f"  skip {skip.stage}: {skip.reason}")
    return 1 if failed else 0


def cmd_call(args: argparse.Namespace) -> int:
    """Run one call through a chain named on the command line.

    The library API in one command: an explicit list or a named chain, a budget, and `--force` for
    a single diagnostic attempt against a parked stage. Exit 2 is a refusal (the request was
    wrong), exit 1 is a call that nobody could serve.
    """
    router = get_router()
    stages = [part.strip() for part in (args.stages or "").split(",") if part.strip()] or None
    options = dict(stages=stages, chain=args.chain, fallback=args.fallback,
                   budget=args.budget, max_cost_usd=args.max_cost, ignore_pause=args.force)
    try:
        if args.capability == "search":
            result = router.run_search(" ".join(args.target), args.limit, **options)
        else:
            result = router.run_extract(list(args.target), **options)
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    router.log_route(args.capability, result, {"source": "cli"})

    if args.json:
        print(json.dumps({
            "ok": result.ok,
            "capability": args.capability,
            "served_by": result.stage or None,
            "chain_mode": result.chain_mode,
            "requested": list(result.requested),
            "budget": result.budget,
            "max_cost_usd": result.max_cost_usd,
            "forced": result.forced,
            "delivered": result.delivered or None,
            "chain": result.chain_text(),
            "payload": result.payload,
        }, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    if not result.ok:
        print(routing.format_failure(result, args.capability))
        return 1
    print(f"{args.capability}: served by {result.stage} ({result.chain_mode}"
          + (f", budget={result.budget}" if result.budget else "")
          + (", forced" if result.forced else "") + ")")
    print(f"  chain: {result.chain_text()}")
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
    # Strip first: a page of spaces passed as "ok" — the same stub that EMPTY exists to catch.
    good = bool(str(entry.get("content") or "").strip())
    print(f"extract {url}: {'ok' if good else 'FAILED'} "
          f"({len(entry.get('content') or '')} chars)")
    print(f"  chain: {extract.chain_text()}")
    ok &= good
    return 0 if ok else 1


def cmd_prune(args: argparse.Namespace) -> int:
    """Drop audit lines older than N days. Safe to run from a timer: it rewrites atomically."""
    settings = config.settings(refresh=True)
    days = args.days if args.days is not None else settings.log_keep_days
    report = retention.prune_directory(settings.state_dir, days, now=time.time())
    if args.json:
        print(json.dumps({"days": days, "files": report}, ensure_ascii=False, indent=2))
        return 0
    if not report:
        print(f"{settings.state_dir}: no audit log yet")
        return 0
    for name, (kept, dropped) in report.items():
        print(f"{name}: kept {kept}, dropped {dropped} line(s) older than {days} days")
    return 0


def run_daemon(interval: float, iterations: int, *, probe: Optional[Callable[[], dict]] = None,
               sleep: Callable[[float], None] = time.sleep,
               out: Callable[[str], None] = print) -> int:
    """Probe vendor balances on a fixed interval, and prune the audit trail once per cycle.

    Native scheduling: the same command works under a systemd timer, in a container, or as
    `quotarouter daemon` in the foreground. `iterations=1` is one cycle and exit, which is what
    a cron line should call — `quotarouter schedule --print` emits both forms.
    """
    def one_cycle() -> dict:
        if probe is not None:
            return probe()
        results = get_router().probe_all(force=True)
        retention.prune_directory(config.settings().state_dir,
                                  config.settings().log_keep_days, now=time.time())
        return results

    cycle = 0
    while iterations <= 0 or cycle < iterations:
        cycle += 1
        try:
            results = one_cycle()
            missing = [name for name, info in results.items() if info.get("remaining") is None]
            out(f"cycle {cycle}: probed {len(results)} vendor(s)"
                + (f", no answer from {', '.join(missing)}" if missing else ""))
        except Exception as exc:  # a daemon must outlive a bad cycle
            out(f"cycle {cycle}: probe failed: {exc}")
        if iterations > 0 and cycle >= iterations:
            break
        sleep(interval)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    settings = config.settings(refresh=True)
    interval = args.interval if args.interval is not None else settings.probe_interval
    return run_daemon(interval, 1 if args.once else 0)


def _default_env_file() -> str:
    """The file to hand systemd, or an empty string when there is nothing worth pointing at.

    `EnvironmentFile` is optional (`-` prefix), so an empty result simply omits the line: a unit
    that sources a nonexistent file is noise, and guessing paths is worse than saying nothing.
    """
    explicit = config.env("ENV_FILE")
    if explicit:
        return explicit
    for candidate in (Path.home() / ".hermes" / ".env",):
        if candidate.exists():
            return str(candidate)
    return ""


SYSTEMD_SERVICE = """[Unit]
Description=quotarouter: refresh vendor quota balances
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={workdir}
ExecStart={python} -m quotarouter.cli daemon --once
{env_line}"""

SYSTEMD_TIMER = """[Unit]
Description=quotarouter: quota refresh timer

[Timer]
OnBootSec=2min
OnUnitActiveSec={interval}
Unit=quotarouter-probe.service

[Install]
WantedBy=timers.target
"""


def cmd_schedule(args: argparse.Namespace) -> int:
    """Print the ready-to-use scheduling units. Nothing is installed: that is the operator's call."""
    settings = config.settings(refresh=True)
    interval = int(args.interval if args.interval is not None else settings.probe_interval)
    python = sys.executable or "python3"
    workdir = str(Path.cwd())
    env_file = _default_env_file()

    hours = interval // 3600
    if interval % 3600 == 0 and 0 < hours <= 24 and 24 % hours == 0:
        cron = f"17 */{hours} * * *"
    elif interval < 3600:
        cron = f"*/{max(1, interval // 60)} * * * *"
    else:
        cron = "17 * * * *"

    env_line = f"EnvironmentFile=-{env_file}\n" if env_file else ""
    service = SYSTEMD_SERVICE.format(workdir=workdir, python=python, env_line=env_line)

    if args.json:
        print(json.dumps({"interval": interval,
                          "cron": f"{cron} cd {workdir} && {python} -m quotarouter.cli daemon --once",
                          "systemd_service": service,
                          "systemd_timer": SYSTEMD_TIMER.format(interval=f"{interval}s")}, indent=2))
        return 0

    print("# 1. cron — one line, one cycle per run")
    print(f"{cron} cd {workdir} && {python} -m quotarouter.cli daemon --once")
    print()
    print("# 2. systemd (user units) — write both files, then: systemctl --user enable --now quotarouter-probe.timer")
    print("# ~/.config/systemd/user/quotarouter-probe.service")
    print(service.rstrip())
    print()
    print("# ~/.config/systemd/user/quotarouter-probe.timer")
    print(SYSTEMD_TIMER.format(interval=f"{interval}s").rstrip())
    print()
    print("# 3. foreground — for a container or a terminal")
    print(f"{python} -m quotarouter.cli daemon --interval {interval}")
    return 0


def cmd_alert_test(args: argparse.Namespace) -> int:
    """Send one alert through the configured transports, ignoring the cooldown."""
    sent = alerts.notify(args.kind, f"quotarouter: test alert ({args.kind})", force=True)
    configured = alerts.settings()
    if not configured.enabled:
        print("no alert transport configured — set QUOTAROUTER_ALERT_TELEGRAM, "
              "QUOTAROUTER_ALERT_WEBHOOK or QUOTAROUTER_ALERT_COMMAND")
        return 1
    print("sent" if sent else "configured, but every transport failed (see the log)")
    return 0 if sent else 1


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    """MCP stdio: line-delimited JSON-RPC 2.0 on stdin/stdout. Nothing else is printed."""
    from .integrations.mcp import serve_stdio

    return serve_stdio()


def _pause(state: ledger_mod.VendorState, now: float) -> str:
    """One column that says both what happened and who is responsible for undoing it."""
    if not state.is_paused(now):
        return "-"
    if ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_OPERATOR:
        return f"stopped: {state.pause_reason or state.pause_kind}"
    left = state.pause_left(now)
    if left == float("inf"):
        # No deadline and not an operator stop: a pause written before machine pauses had one.
        # The next call re-probes the balance and replaces this with a real retry deadline.
        return f"{state.pause_kind} (machine, re-probed)"
    hours = left / 3600.0
    span = f"{hours:.1f}h" if hours >= 1 else f"{int(left // 60)}m"
    return f"{state.pause_kind} {span} (auto)"


def _iso(ts: Optional[float]) -> str:
    if ts is None or ts == float("inf"):
        return "-"
    import datetime as dt

    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quotarouter",
        description="Quota-aware web search/extract router: spend free allowances first.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show what is left in every vendor's free pool")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    probe = sub.add_parser("probe", help="ask vendors for their real remaining quota and store it")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=cmd_probe)

    recheck = sub.add_parser(
        "recheck",
        help="re-probe parked vendors and release the pauses the machine took, now")
    recheck.add_argument("--json", action="store_true")
    recheck.set_defaults(func=cmd_recheck)

    prune = sub.add_parser("prune", help="drop audit lines older than N days")
    prune.add_argument("--days", type=int, default=None,
                       help="keep this many days (default: QUOTAROUTER_LOG_KEEP_DAYS or 30)")
    prune.add_argument("--json", action="store_true")
    prune.set_defaults(func=cmd_prune)

    daemon = sub.add_parser("daemon", help="probe balances on an interval, forever")
    daemon.add_argument("--interval", type=float, default=None,
                        help="seconds between cycles (default: QUOTAROUTER_PROBE_INTERVAL or 21600)")
    daemon.add_argument("--once", action="store_true", help="one cycle and exit (what cron calls)")
    daemon.set_defaults(func=cmd_daemon)

    schedule = sub.add_parser("schedule", help="print cron/systemd units for periodic probing")
    schedule.add_argument("--interval", type=float, default=None)
    schedule.add_argument("--json", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    alert_test = sub.add_parser("alert-test", help="send one alert through the configured transport")
    alert_test.add_argument("--kind", default="keyless", choices=[alerts.NO_STAGE, alerts.KEYLESS, alerts.QUOTA])
    alert_test.set_defaults(func=cmd_alert_test)

    route = sub.add_parser("route", help="show which vendor a call would use now, and why not others")
    route.add_argument("--stages", default=None, help="comma-separated stage list to resolve")
    route.add_argument("--chain", default=None, help="named chain from the configuration file")
    route.add_argument("--fallback", action="store_true", help="continue into the default order")
    route.add_argument("--budget", default=None, help="'free' to forbid spending money")
    route.add_argument("--max-cost", dest="max_cost", type=float, default=None,
                       help="dollar ceiling for one call")
    route.set_defaults(func=cmd_route)

    chains = sub.add_parser("chains", help="show the named chains the configuration produced")
    chains.add_argument("--json", action="store_true")
    chains.set_defaults(func=cmd_chains)

    call = sub.add_parser("call", help="run one call through a chain you name")
    call.add_argument("capability", choices=["search", "extract"])
    call.add_argument("target", nargs="+", help="a query, or one or more URLs")
    call.add_argument("--limit", type=int, default=5)
    call.add_argument("--stages", default=None, help="comma-separated stage list")
    call.add_argument("--chain", default=None, help="named chain from the configuration file")
    call.add_argument("--fallback", action="store_true")
    call.add_argument("--budget", default=None)
    call.add_argument("--max-cost", dest="max_cost", type=float, default=None)
    call.add_argument("--force", action="store_true",
                      help="one attempt against a parked stage (it is charged and re-parked)")
    call.add_argument("--json", action="store_true")
    call.set_defaults(func=cmd_call)

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

    serve = sub.add_parser("serve-mcp", help="run as an MCP stdio server (for any MCP client)")
    serve.set_defaults(func=cmd_serve_mcp)

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
