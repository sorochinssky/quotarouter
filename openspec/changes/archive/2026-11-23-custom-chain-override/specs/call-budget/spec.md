# Spec Delta

## Purpose

What a single call is allowed to spend: a free-only mode that never touches money, a cost ceiling
that refuses a call before a vendor is dialed, and the per-vendor declaration that makes "free" a
fact instead of an inference. A budget bounds one call; it does not set research depth.

## ADDED Requirements

### Requirement: Every stage declares how it is funded

Each stage MUST declare whether its quota is free (unmetered, or a free grant for the current
period) or paid (prepaid balance). The declaration MUST be available to the selector and to the
status surface, because "has quota left" and "has free quota left" are different questions.

#### Scenario: a balance-funded stage is not advertised as free
- **WHEN** the status surface lists stages
- **THEN** each stage shows its funding kind next to its remaining quota

### Requirement: The free-only budget never spends money

A call made with the free-only budget MUST select only stages whose current consumption is inside
their free grant or that are unmetered. A balance-funded stage MUST NOT serve such a call. A stage
whose free grant is spent MUST be skipped rather than allowed to spill into paid usage. Every
skipped stage MUST carry a reason that names the cause.

#### Scenario: a balance-funded stage never serves a free call
- **WHEN** a free-only search runs while a prepaid-balance vendor has funds and the free-grant
  vendors do not
- **THEN** the call fails with a per-stage reason instead of spending the balance

#### Scenario: an exhausted free grant is not overrun
- **WHEN** a free-only search runs and the first stage's free grant is spent
- **THEN** that stage is skipped with a reason, and the next free stage serves

### Requirement: The anonymous tier counts as free

The free-only budget MUST allow the anonymous tier, unless a strict chain forbids it. The budget
expresses "no money", not "one vendor"; the chain decides how narrow the choice is.

#### Scenario: an anonymous answer is acceptable under a free budget
- **WHEN** a free-only call is made with fallback and every key-backed free stage refuses
- **THEN** the anonymous tier may serve the call

### Requirement: A cost ceiling refuses before dialing

A caller MUST be able to set a maximum cost for one call. The router MUST estimate the cost of
what the call asks for from the stages' own declared unit costs and MUST refuse the call before any
vendor is dialed when the estimate exceeds the ceiling. The refusal MUST name the estimate and the
ceiling. Where an exact cost is not knowable in advance, the estimate MUST be documented as an
upper bound.

#### Scenario: too expensive means no request
- **WHEN** a call asks for more pages than the ceiling allows
- **THEN** the call fails before any network call, and the message states the estimate and the ceiling

### Requirement: Budget and chain are validated in one step, before the ledger moves

A budget that is malformed, a chain that cannot satisfy the budget, and a ceiling below the cheapest
available stage MUST all be caller errors raised before the first network call, leaving the ledger
unchanged. A protocol surface MUST return them as tool errors and MUST remain available.

#### Scenario: an impossible budget is a caller error
- **WHEN** a caller asks for a free-only call with a strict chain of one balance-funded stage
- **THEN** the call is refused before any network call, and the ledger is unchanged

### Requirement: A dollar ceiling does not price what it cannot price

A cost ceiling MUST estimate only stages whose unit is money. A stage metered in vendor credits or
requests MUST be treated as under the free-only budget: usable inside its free grant, refused once
the grant is spent. The router MUST NOT convert credits or requests into money, and MUST NOT ignore
unpriceable stages when comparing the estimate with the ceiling. A refusal caused by this rule MUST
say so in one line.

#### Scenario: a credit-metered stage is not priced at a guess
- **WHEN** a call sets a dollar ceiling and the first stage of the chain spends vendor credits
- **THEN** that stage serves only inside its free grant, and after the grant is spent it is skipped
  with a reason naming the rule

### Requirement: A caller may tighten a chain's budget but never loosen it

When a chain declares a budget, a call MUST be able to ask for a stricter one, and MUST NOT be able
to relax it. The applied budget MUST be the stricter of the chain's and the call's, and the audit
record MUST show the budget that was actually applied.

#### Scenario: a free chain stays free when handed a ceiling
- **WHEN** a call asks for a chain that declares a free-only budget together with a dollar ceiling
- **THEN** the free-only budget applies, and no balance-funded stage is dialed

### Requirement: The budget of a call is recorded

Every call MUST record the budget it ran under (free-only, a ceiling, or none) next to the chain it
used, so that cost questions can be answered from the audit file alone.

#### Scenario: a free call is identifiable afterwards
- **WHEN** a free-only search completes
- **THEN** its audit record states the free-only budget
