# Spec Delta

## Purpose

How the router decides which stages may serve one call: the default order, a named chain from
configuration, or an explicit list given by the caller — with one precedence rule, strict failure
instead of a silent fallback, and the same accounting for every path.

## ADDED Requirements

### Requirement: Each call resolves exactly one chain before any network call

A call MUST resolve a single ordered list of stages before it dials anything, from exactly one of
three sources: an explicit list, a named chain, or the default order for the capability. A caller
MUST NOT be able to supply both an explicit list and a name; that request MUST be rejected rather
than silently resolved.

#### Scenario: the default order is used when nothing is asked for
- **WHEN** a search runs with no chain argument
- **THEN** the stages are the configured search order, and the audit record says `chain_mode: default`

#### Scenario: an explicit list and a name are mutually exclusive
- **WHEN** a caller passes both an explicit list and a chain name
- **THEN** the call fails with an error naming both arguments and no vendor is dialed

### Requirement: A custom chain is strict

When a caller names stages or a chain, only those stages may serve the call. The router MUST NOT
continue into the default order and MUST NOT answer from the anonymous tier; if none of the named
stages can serve, the call MUST fail with a reason per stage.

#### Scenario: one stage means one stage
- **WHEN** a search runs with exactly one stage and that stage is parked or out of quota
- **THEN** the call fails and no other vendor is dialed

#### Scenario: no anonymous tier behind a strict chain
- **WHEN** every stage of a custom chain refuses and the anonymous tier is enabled globally
- **THEN** the call fails and the anonymous tier is not used

### Requirement: Falling back to the default order is opt-in

A caller MUST be able to ask for "these stages first, then behave normally". With that flag, once
the custom stages are exhausted the router continues with the default order for the capability,
skipping names already tried, and the end-of-chain policy of that capability applies — including
whether the anonymous tier may serve.

#### Scenario: preferred stage first, default afterwards
- **WHEN** a search asks for one preferred stage with fallback enabled and that stage refuses
- **THEN** the remaining default stages are tried in their own order, each at most once

### Requirement: An invalid chain is refused before anything is charged

An unknown stage name, a stage that does not support the requested capability, an empty list, a
malformed chain specification or an unknown chain name MUST be reported as a caller error before
any vendor call, and MUST NOT change the ledger. A protocol surface MUST return this as a tool
error and MUST remain available for the next request.

#### Scenario: a typo is a caller error, not a vendor error
- **WHEN** a call names a stage that does not exist
- **THEN** no network call happens, the ledger is unchanged, and the error lists the names that do exist

#### Scenario: a capability mismatch is refused
- **WHEN** a caller asks an extract call to use a search-only stage
- **THEN** the call is refused before any network call

### Requirement: Quotas, pauses and accounting apply to every chain

A custom call MUST go through the same ledger, the same pause rules and the same charging as a
default call: it can park a stage, be skipped for a pause, and spend the vendor's quota. A caller
MUST be able to force a single attempt against a parked stage for diagnosis; a forced attempt MUST
be recorded as forced, MUST still be charged, and MUST re-park the stage with the vendor's own
reason if it fails.

#### Scenario: a forced probe does not launder the ledger
- **WHEN** a parked stage is called with the force flag and the vendor answers "quota exhausted" again
- **THEN** exactly one attempt is recorded as forced, and the stage stays parked for that reason

### Requirement: Named chains are configuration, validated once

A configuration value MUST be able to declare named chains as an ordered mapping of name to
stages. A malformed declaration MUST be reported and ignored without preventing startup; an
unknown capability or stage inside a named chain MUST be treated as a caller error when that name
is requested. Names MUST be listed wherever the caller can discover them (status output and the
protocol surface's status tool).

#### Scenario: a bad declaration does not stop the process
- **WHEN** the chain configuration contains one valid name and one malformed entry
- **THEN** the process starts, the valid name is usable, and the malformed entry is reported

### Requirement: The operator can disable per-call overrides

A deployment MUST be able to refuse per-call chain overrides entirely. When overrides are
disabled, a request that asks for one MUST be refused with a reason naming the setting; the router
MUST NOT silently serve the default order instead.

#### Scenario: refusal, not substitution
- **WHEN** overrides are disabled and a caller asks for a specific chain
- **THEN** the call fails with an error naming the setting, and no vendor is dialed

### Requirement: The audit trail names the chain that was used

Every call MUST record whether the chain was the default, an explicit list or a name, what was
requested, and whether an attempt was forced. Existing readers of the audit file MUST keep working:
the new fields are additive.

#### Scenario: a custom call is identifiable afterwards
- **WHEN** a search served by an explicit single stage completes
- **THEN** the audit record carries the custom mode, the requested name and the stage that served it

### Requirement: A custom call does not change what the next call does

Resolving and running a custom chain MUST NOT modify the configured default order, and two calls
with different chains running at the same time MUST NOT affect each other's choice.

#### Scenario: the default survives a custom call
- **WHEN** a call with an explicit chain completes, and then a call with no chain argument runs
- **THEN** the second call uses the configured default order

#### Scenario: concurrent callers keep their own chains
- **WHEN** two calls with different explicit chains run concurrently
- **THEN** each dials only its own stages
