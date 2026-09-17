# Top-5 controlled shadow run preparation

Status: `PREPARATION ONLY`, `NO NETWORK`, `NO BET`, `UNPUBLISHED`,
`CANDIDATE_ONLY`.

`ControlledShadowRunPreparationV1` is a deterministic, no-network planning
artifact for a future Top-5 controlled shadow observation. It does not execute
provider calls, authorize a run, issue a Builder-2 qualification receipt, write
the ledger, publish data, or activate production behavior.

## Safety boundary

Every CLI invocation prints these safety banners:

```text
PREPARATION ONLY
NO NETWORK
NO BET
NO PUBLICATION
NO PRODUCTION ACTIVATION
CEO AUTHORIZATION REQUIRED FOR REAL EXECUTION
```

`READY_FOR_CEO_CONTROLLED_SHADOW_AUTHORIZATION` means that at least one
configured provider has a technically executable future observation plan. It
is not CEO authorization. The artifact always records
`authorization_status=NOT_AUTHORIZED` and `execution_allowed=false`.

Builder 2 is not a preparation input. There is no circular receipt before
capture, and Builder 4 does not issue or manufacture
`Builder2QualificationReceiptV1`. After a real capture, the canonical
Builder-2 receipt contract remains the sole qualification authority consumed
by Builder 1 and Builder 4.

Preparation artifacts may be written only to an absolute caller-selected
external or test path. Checkout paths and ledger, source, script, test, and
published-data locations are rejected.

## Input contract

The `prepare` command accepts a JSON object containing:

- `fixture`: fixture key, Top-5 league code, exact home/away teams, and UTC
  kickoff;
- `timing_policy`: explicit `maximum_odds_age_seconds` and
  `kickoff_tolerance_seconds`;
- `provider_order`: the canonical singleton sequence containing only
  `the_odds_api`;
- `credential_presence`: per-provider booleans, never credential values;
- `provider_readiness`: caller-supplied readiness, identity, fixture, quota,
  and provider-contract evidence;
- `maximum_total_network_requests` and
  `maximum_total_quota_cost_units`;
- optional per-provider request/cost maximums and safety flags, which must
  remain disabled.

The supported provider order is fixed and preserved exactly. The planner does
not rank providers or infer a preferred source:

1. `the_odds_api` — sport-level odds response, with fixture identity resolved
   from the response;
The Odds API known baseline of `used=500`, `remaining=0` produces zero planned
requests. Unknown credentials, readiness, identity, quota, malformed fixture
or timing fields, duplicate/unsupported providers, invalid budgets, and
ambiguous identity all fail closed. `UNKNOWN` never becomes `READY`; no
alternate-provider state exists.

## Output contract

The artifact contains a manifest for the canonical provider. Each manifest records
configured position, candidate-only state, credential-presence boolean,
fixture/discovery state, identity/readiness state, quota snapshot, expected
endpoint/action class, request and quota-cost maxima, timestamp capability,
delayed semantics, bookmaker constraints, expected failure classes,
prerequisites, and the bounded planned actions.

The run-level artifact additionally records:

- exact fixture and timing identity plus timing digest/reference;
- preserved sequential execution plan and total request/cost budgets;
- known blockers and CEO decisions still required;
- deterministic preparation ID and SHA-256 preparation digest;
- zero monetary spend, no-bet, unpublished, non-production, and unsealed
  invariants;
- explicit no-network and discovery/odds-separation guarantees.

All actions are plans only. A future executor would still need a separate,
explicit CEO authorization context for the specific run. No authorization
fields in this artifact can enable execution.

## CLI

```bash
python3 scripts/top5_controlled_shadow_preparation.py \
  prepare /absolute/path/fixture-manifest.json \
  --output /absolute/path/external/preparation.json

python3 scripts/top5_controlled_shadow_preparation.py \
  inspect /absolute/path/external/preparation.json

python3 scripts/top5_controlled_shadow_preparation.py \
  validate /absolute/path/external/preparation.json
```

`prepare` and `inspect` emit JSON on stdout and safety banners on stderr.
`validate` reloads the artifact, verifies its digest and all contract
invariants, and returns a non-zero status on any malformed or unsafe state.
