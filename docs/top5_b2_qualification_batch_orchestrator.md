# Top-5 Builder-2 Qualification Batch Orchestrator V1

The batch orchestrator processes multiple already-captured canonical Builder-2
capture/intake packages in deterministic order. It delegates all authority
decisions to the existing contracts:

```text
capture manifest
  → existing qualification gate
  → existing Builder2QualificationReceiptV1 issuer
  → existing sample aggregator
  → existing Decision Packet
```

It does not execute a provider or a controlled shadow run. It does not create
CEO authorization, select a provider or signal time, bind a model, publish,
bet, deploy, mutate the ledger, or change source evidence.

## Input

An input directory must be an absolute external directory containing one or
more package directories. Each package has `manifest.json` and may include
canonical `qualification_report.json`, `receipt.json`, `result.json`, and
`sample_report.json` artifacts. A single absolute `manifest.json` or JSON
package is also accepted. Unknown JSON files and malformed canonical artifacts
are rejected fail-closed.

The manifest binds the controlled-shadow run, CEO authorization, fixture,
league, provider, provider event/request, observation and normalized digests,
capture attestation, adapter provenance, qualification session, cascade, and
intake identity. Missing or ambiguous bindings never receive a synthetic
completion.

## Usage

```text
python3 scripts/top5_b2_qualification_batch.py \
  /absolute/external/top5-evidence \
  --output-json /absolute/external/batch.json \
  --output-markdown /absolute/external/batch.md \
  --minimum-real-observations 10 \
  --minimum-distinct-fixtures 10
```

The two sample-policy values are optional, but must be supplied together. They
are caller-owned measurement only. There is no production minimum and a
sample-sufficient result never authorizes production.

## Cross-item safety

Exact evidence replays are idempotently recognized and do not issue another
receipt. Divergent receipts, observation digests, run/auth/session bindings,
capture attestations, fixture/provider identities, or intake identities are
retained as conflicts and excluded from accepted sample evidence. Independent
valid items continue to process.

The JSON and Markdown result includes item states, failure taxonomy, receipt
IDs/digests, qualification/session/run/authorization identities, provider and
league counts, duplicate/conflict/replay evidence, the canonical sample report,
the canonical Decision Packet, unresolved items, and immutable safety flags:

```text
production_activation_authorized = false
controlled_activation_authorized = false
publication_authorized = false
betting_authorized = false
model_authorized = false
signal_time_authorized = false
provider_network_execution = false
```

## Canonical B4 five-league one-shot path

For the completed B4 controlled-shadow package, use the dedicated five-league
mode. The input is one external JSON package containing the canonical
`TheRundownNetworkShadowRunResultV1`, full target/request/capture envelopes for
all five captures, and five `Builder2QualificationIntakeManifestV1` objects.
The package must cover exactly `BL1`, `EPL`, `LL`, `SA`, and `L1`; it must be
`COMPLETED_NETWORK` and pass the existing B4-to-B2 intake gate.

```text
python3 -m src.football.top5_b2_qualification_batch_orchestrator \
  --five-league-input /absolute/external/b4-five-league-package.json \
  --five-league-output /absolute/external/b2-five-league-receipts.json
```

This path performs no network or provider work. It either derives all five
`Builder2QualificationReceiptV1` objects and one deterministic,
non-authorizing Builder-3 input dossier, or writes no new output. The single
output JSON is the atomic commit unit; an existing identical output is an
idempotent no-op and a conflicting output path fails closed.

The dossier contains the five receipt IDs/digests and qualification-result
digests, provider/run/session/CEO identities, configuration and adapter source
SHA bindings, per-league fixture/event/request/participant/bookmaker identities,
complete 1X2, kickoff/source/captured timestamps, raw/provider/normalized/cascade
and attestation digests, quota/datapoint/rate-limit/tier/delay/HTTP/retry
provenance, source-artifact digests, and an overall dossier digest. It
explicitly carries no provider authority, publication, activation, betting,
model, or signal-time authorization.

The output is deterministic: the same valid B4 package produces the same five
receipt identities, receipt digests, dossier identity, and dossier digest.
Missing or duplicate leagues, synthetic/replay evidence, mixed authorization
identities, altered observations, stale or contradictory quota evidence, and
any provider other than the candidate-only identity fail before the output
file is committed. The canonical league order in the dossier is `BL1`, `EPL`,
`LL`, `SA`, `L1`.
