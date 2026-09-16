# Top-5 real-shadow evidence audit

`top5_real_shadow_audit.py` is a deterministic, read-only consumer of
already-created Top-5 shadow artifacts. It does not call a provider, issue a
Builder-2 receipt, create authorization, generate a prediction, publish, bet,
write a ledger, or modify the active checkout.

The auditor follows the chain:

```text
Builder2QualificationReceiptV1
  -> canonical REAL_OBSERVED observation
  -> RealShadowSession
  -> RealShadowPredictionArtifact
  -> result attachment
  -> closing attachment
  -> optional top5-shadow-evidence-v1 bundle
  -> optional Builder-2 sample report
```

It never upgrades `TEST_FIXTURE` or `OFFLINE_REPLAY` into real evidence.
Missing evidence is reported explicitly as `PENDING_RESULT`,
`PENDING_CLOSING`, `QUALIFICATION_MISSING`, or `INCOMPLETE`; unknown input
does not become `COMPLETE`.

## CLI

```text
python3 scripts/top5_shadow_audit.py session session.json --evidence evidence.json
python3 scripts/top5_shadow_audit.py prediction prediction.json
python3 scripts/top5_shadow_audit.py directory shadow-artifacts/
python3 scripts/top5_shadow_audit.py inspect audit.json
```

Each command is local-only and prints machine-readable JSON plus a concise
Markdown summary by default. Use `--format json` or `--format markdown` after
the subcommand when one representation is needed.

The output contains non-secret identities, digests, completeness flags,
findings, deterministic ordering, and a deterministic audit digest. It does
not include provider response bodies, credentials, headers, cookies, tokens,
or API keys. Evidence counts are audit counts only; no model quality,
profitability, production readiness, or activation claim is produced.
