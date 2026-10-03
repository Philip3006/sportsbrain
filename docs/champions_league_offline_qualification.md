# Champions League offline qualification contract

`src/football/champions_league_qualification.py` exposes a deterministic,
side-effect-free boundary for qualifying one offline Champions League
candidate.  `qualify_offline_candidate(...)` returns a report and never
authorizes deployment, publication, betting, or production activation.

Acceptance is fail-closed.  The report is accepted only when all of these
are present, hash-valid, and mutually bound to the same candidate:

- exact candidate artifact identity;
- feature schema identity;
- an approved UTC information-time boundary;
- exact training partition identity;
- non-empty passing calibration evidence;
- non-empty legitimate, held-out final-evaluation evidence;
- source provenance for every evidence family;
- a passing zero-overlap/zero-leakage contamination check;
- exact runtime input names/configuration; and
- a tested rollback target with its exact identity.

Evidence may not silently substitute a candidate, schema, partition, source,
runtime, or rollback target.  Missing fields, digest mismatches, duplicate
evidence IDs, future information, closing-market inputs, unapproved
boundaries, failed calibration, non-held-out evaluation, contamination, and
untested rollback all produce a rejected report.  A rejected report remains
serializable for audit, but `validate_offline_qualification()` raises
`ChampionsLeagueQualificationError`.

Example shape:

```python
from src.football.champions_league_qualification import (
    qualify_offline_candidate,
    validate_offline_qualification,
)

report = qualify_offline_candidate(
    candidate_artifact_identity=candidate,
    feature_schema_identity=feature_schema,
    approved_information_time_boundary=boundary,
    training_partition_identity=training_partition,
    calibration_evidence=(calibration,),
    final_evaluation_evidence=(final_evaluation,),
    source_provenance=(training_source, calibration_source, evaluation_source),
    contamination_pass=contamination,
    runtime_inputs=runtime,
    rollback_identity=rollback,
)
validate_offline_qualification(report)
```

The contract is evidence-only and has no network, provider, scheduler,
publisher, ledger, model-loading, or deployment capability.
