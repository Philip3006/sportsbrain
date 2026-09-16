# Top-5 B2 Controlled Shadow Decision Packet

The independent Builder-2 decision packet is a deterministic, read-only CEO
review projection over already existing evidence. It consumes only:

- `ProviderQualificationReport` artifacts;
- `Builder2QualificationReceiptV1` artifacts;
- `Builder2QualificationSampleReportV1` artifacts; and
- complete Builder-2 intake directories containing `manifest.json`,
  `qualification_report.json`, `receipt.json`, and `result.json`.

It does not issue receipts, create CEO authorization, execute a controlled
shadow run, make provider calls, generate predictions, select a provider or
signal time, bind a model, publish, bet, deploy, or mutate production state.

## CLI

The input directory must be an absolute external evidence directory. Output
paths, when supplied, must also be absolute and external to the repository:

```text
python3 scripts/top5_b2_decision_packet.py \
  /absolute/external/top5-evidence \
  --output-json /absolute/external/decision-packet.json \
  --output-markdown /absolute/external/decision-packet.md \
  --minimum-real-observations 10 \
  --minimum-distinct-fixtures 10
```

The two policy values are caller-owned and must be supplied together. There is
no default or production sample threshold. If receipts are present, the
packet derives the measurement through the existing receipt-only aggregator;
this does not issue or change any authority. A supplied sample report is
validated and its provenance is compared with the canonical receipt set.

## Packet contents

`Builder2QualificationDecisionPacketV1` emits deterministic JSON and concise
Markdown containing the packet ID/digest, evidence state, sample state,
controlled-shadow run/session/authorization identities, report summaries,
receipt provenance, eligible real observation and fixture counts, provider and
league counts, duplicate/conflict/exclusion taxonomy, caller policy and
sample sufficiency, completeness reasons, unresolved items, CEO decisions,
and source-artifact digests.

The packet distinguishes `EVIDENCE_COMPLETE` from `EVIDENCE_INCOMPLETE`,
`EVIDENCE_CONFLICT`, and `AUTHORITY_MISMATCH`. `SAMPLE_MEETS_CALLER_POLICY`
and `SAMPLE_BELOW_CALLER_POLICY` are measurement states only. Malformed or
unsafe serialized input is rejected fail-closed rather than silently treated
as complete; unknown artifacts and orphan intake files are rejected.

## Immutable safety boundary

The output always carries these false authority fields:

```text
production_activation_authorized = false
publication_authorized = false
betting_authorized = false
model_authorized = false
signal_time_authorized = false
controlled_activation_authorized = false
```

`sample_sufficient` never authorizes production. CEO decisions remain required
for any future signal-time selection, production-model binding, publication,
betting, or Controlled Activation. Freshness and coverage remain limited to
what the canonical input artifacts actually bind; no denominator is invented.
