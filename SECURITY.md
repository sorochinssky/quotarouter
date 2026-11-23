# Security policy

## Reporting a vulnerability

Report privately through GitHub's
[Security Advisories](https://github.com/sorochinssky/quotarouter/security/advisories/new) or by
email to the address on the author's GitHub profile. Please do not open a public issue for
anything that involves a way to read, log, or transmit a credential.

Include the version (`python -c "import quotarouter; print(quotarouter.__version__)"`), the stage
involved, and a reproduction that uses placeholder keys. You will get an answer within a week.

## What this package does with secrets

- **Keys come from the environment or a dotenv file** (`QUOTAROUTER_ENV_FILE`, `./.env`,
  `~/.config/quotarouter/.env`). They are read into memory and sent to the vendor they belong
  to. Nothing else receives them.
- **Keys are never written to the ledger, the audit log, or an alert.** The ledger stores the
  balance each vendor reported, the pause state, and the local estimate — no credentials.
- **The audit log (`routes.jsonl`) records the query and the URLs** you asked for, plus which
  stage served them. It is a local file under `QUOTAROUTER_STATE_DIR`; treat it as you would any
  other log of user input.
- **Alerts carry a short reason string**, not request bodies.

If a vendor key leaks into an error message, that is a bug worth reporting: stage errors are
truncated to 300 characters and pass through the vendor's own text.

## Supported versions

The latest release on PyPI. Fixes for credential handling are backported to the previous minor
version when the change is small enough to be safe.

## Scope notes

The router executes an operator-configured command for the `command` alert transport. That is
deliberate and documented; it is not a vulnerability that the package runs the command you
configure. Reports about a malicious *vendor* response are in scope: parsing hostile JSON is
this package's job.
