# Champions League dataset qualification

`src/football/champions_league_qualification.py` is an independent,
deterministic qualification boundary for UEFA Champions League data. It does
not call providers, run a model, publish an artifact, or consult Builder 1.
Callers provide rows and artifacts; the contract either accepts the complete
set or rejects it.

## Dataset contract

Each row must carry the canonical competition identity, a stable fixture and
observation identity, a `YYYY-YY` season, the matching historical format era,
stage and round, canonical home/away team identities, kickoff time, venue
semantics, source availability, result status, and one of the development,
calibration, or holdout partitions. Accepted historical aliases are normalized
to `uefa_champions_league`; the serialized payload always uses that ID.

The historical eras are explicit:

| Season start | Format era |
| --- | --- |
| before 1992 | European Cup |
| 1992–2002 | first Champions League group-stage era |
| 2003 | two-group-stage era |
| 2004–2023 | group-stage era |
| 2024 onward | league-phase era |

Two-leg ties must contain both legs. Leg 2 must carry an available aggregate
context whose scores agree with leg 1, including when home and away reverse.
Finals must retain neutral-venue semantics. Team display names and provider IDs
must agree through the explicit alias registry; an unregistered disagreement is
rejected rather than guessed.

Rows are rejected for duplicate observations, duplicate canonical fixtures, or
conflicting records sharing a fixture identity. Result and source timestamps
must be chronological. The dataset manifest binds its row digest, source
digest, row count, seasons, eras, and partition counts to the exact rows.

## Temporal and artifact contract

Development, calibration, and holdout windows are required, non-overlapping,
and ordered chronologically. Every row kickoff must be inside its declared
window. Feature provenance must show that the source, source event, and
generated feature existed no later than prediction time and before kickoff;
referencing a target/future fixture or later partition is rejected.

Model metadata binds the model artifact, feature schema, dataset manifest, and
the fixed partition roles. Feature names and market declarations containing
future or historical closing-market inputs are rejected. Shadow evidence must
be pre-kickoff, hash-bound to the model and manifest, complete for the declared
feature set, and explicitly remain no-bet, unpublished, and ledger-neutral.

The public entry points are:

```python
from src.football.champions_league_qualification import (
    qualify_champions_league,
    validate_champions_league_qualification,
)

report = qualify_champions_league(dataset, model_metadata, shadow_evidence)
validate_champions_league_qualification(dataset, model_metadata, shadow_evidence)
```

`qualify_champions_league` returns a deterministic rejection report;
`validate_champions_league_qualification` raises
`ChampionsLeagueQualificationError` on any failed gate.
