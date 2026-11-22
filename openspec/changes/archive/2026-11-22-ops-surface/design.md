# Design

## Alerts: three transports and a cooldown, no monitoring stack

A dependency-free alerting path was the constraint: this package is installed into an agent, not
into a fleet with Prometheus. So the transports are the three a single host already has — the
Telegram Bot API (the owner's chat), a plain webhook (anything else), and a shell command
(`notify-send`, `curl`, a script).

Two properties matter more than features:

* **A cooldown per kind, persisted.** A vendor that is down would otherwise emit one message per
  call — hundreds a day — and the human learns to ignore them. The cooldown map lives in
  `<state>/alerts.json`, so a restart does not reopen the floodgates.
* **Delivery never breaks a call.** Every transport is wrapped: a failure is logged at warning
  level, `False` is returned, and the search goes on delivering its answer. The alternative — an
  alert that can fail a request — makes the alert the outage.

The kinds are the three ways the chain goes off its normal path: nothing served (`no-stage`), the
anonymous tier served (`keyless`), a vendor parked itself (`quota`). Anything finer would be log
analysis, not an alert.

## Retention: two mechanics because they have different costs

Rotation is one `stat` per write and an atomic rename; it belongs in the logging path, where it
cannot be forgotten. Age-based pruning rewrites the file and must not happen mid-call, so it is a
command a timer calls. One rotation generation (`.1`) is deliberate: the goal is a bounded file,
not an archive — archiving belongs in a log shipper.

Prune keeps lines whose timestamp cannot be parsed. The alternative (drop unparsable lines)
silently deletes exactly the malformed evidence an incident investigation needs.

## Parallelism: a pool per stage call, not an async chain

The chain is sequential on purpose: stage two must not run before stage one fails, or the whole
quota discipline collapses. Inside one stage call there is no such coupling — six URLs are six
independent requests — so `_map_urls` fans them out over `EXTRACT_WORKERS` threads and preserves
the caller's order.

Threads, not asyncio: the adapters are synchronous `httpx` calls, and rewriting the chain to be
async would buy nothing here while making every adapter harder to reason about. `1` disables the
pool entirely, which keeps the old behaviour available for debugging and for vendors that
rate-limit per connection.

## Scheduling: print, do not install

`quotarouter schedule` emits a cron line and a systemd service+timer, and stops there. Installing
a timer is a decision about a host, not a fact about a library; a package that silently writes
`~/.config/systemd/user/` on import is a package you cannot trust in a container.

`daemon --once` is the cron entry point (one cycle, exit code from the probe), `daemon
--interval` the foreground loop for containers and tmux. Each cycle also prunes the audit trail,
so a single scheduled command keeps the state directory honest.
