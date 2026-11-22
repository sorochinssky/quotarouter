# ops-surface

## ADDED Requirements

### Requirement: Degradation is reported to a human

The router MUST notify when a call is served off its normal path: nothing served (`no-stage`),
the keyless tier served (`keyless`), or a vendor parked itself out of quota (`quota`).
Notification MUST be off unless a transport is configured through `QUOTAROUTER_ALERT_TELEGRAM`,
`QUOTAROUTER_ALERT_WEBHOOK` or `QUOTAROUTER_ALERT_COMMAND`.

#### Scenario: keyless service raises an alert
- **WHEN** a search is served by the keyless tier
- **THEN** an alert of kind `keyless` is delivered once, and a second identical alert inside the
  cooldown window is suppressed

### Requirement: Alerts are rate-limited and cannot fail a call

Each alert kind MUST honour a cooldown whose last-sent time survives a restart
(`<state>/alerts.json`), so a dead vendor cannot emit one message per call. A failing transport
MUST be logged and MUST NOT fail the call it was reporting on.

#### Scenario: keyless service raises an alert
- **WHEN** a search is served by the keyless tier
- **THEN** an alert of kind `keyless` is delivered once, and a second identical alert inside the
  cooldown window is suppressed

#### Scenario: a broken transport does not fail the search
- **WHEN** the configured alert command does not exist
- **THEN** the call still returns its result and the failure is logged as a warning

### Requirement: The audit trail stays bounded without a cron job for rotation

`routes.jsonl` MUST rotate when it exceeds `QUOTAROUTER_LOG_MAX_BYTES`, and the rotation MUST
happen in the logging path so no external scheduler is required. Age-based trimming MUST be
available as `quotarouter prune --days N` and MUST keep lines whose timestamp cannot be parsed.

#### Scenario: rotation inside the write path
- **WHEN** the audit file passes the configured size
- **THEN** the file becomes `routes.jsonl.1` and a fresh `routes.jsonl` starts, with no
  configuration outside the process

#### Scenario: prune keeps unparsable evidence
- **WHEN** `prune` runs over a file containing an old line, a fresh line and a malformed line
- **THEN** only the old line is dropped

### Requirement: Probing can be scheduled without the package installing anything

The package MUST provide a probe loop (`daemon --once` for one cycle, `daemon --interval` to
continue) and MUST be able to print ready-to-use cron and systemd units. It MUST NOT install a
timer, a cron entry or a bot on its own.

#### Scenario: schedule prints, does not install
- **WHEN** `quotarouter schedule` runs
- **THEN** it prints a cron line and a systemd service+timer and writes nothing to disk

### Requirement: One extract call fetches its pages concurrently

Fetching several URLs for one stage call MUST run in a bounded pool
(`QUOTAROUTER_EXTRACT_WORKERS`, default 4, `1` meaning sequential) and MUST preserve the order of
the requested URLs. The stages themselves MUST remain sequential.

#### Scenario: two pages overlap
- **WHEN** two URLs are extracted with `EXTRACT_WORKERS=2`
- **THEN** both requests are in flight at the same time (proved by a barrier that a sequential
  implementation cannot pass) and the returned order matches the requested order
