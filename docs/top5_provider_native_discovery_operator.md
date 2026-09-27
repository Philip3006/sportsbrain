# Top-5 provider-native Discovery operator path

This is the single current operator entrypoint for fresh TheRundown Top-5
Discovery:

```sh
python -m src.football.top5_therundown_provider_native_discovery run \
  --quota-proof-package /absolute/path/to/current-b4-quota-proof.json \
  --discovery-authorization-id '<CEO-approved authorization ID>' \
  --ceo-discovery-authorization-identity '<CEO authorization identity>' \
  --issued-at '<RFC3339 UTC issue time>' \
  --expires-at '<RFC3339 UTC expiry time>'
```

That is a dry run. It validates the current B4 proof and materializes the
current structural contract in memory, but makes no provider request, reads no
credential, and consumes no one-shot marker. The generated result identifies
the authorization digest and the exact code-derived limits. No schema version,
purpose, league, request digest, source SHA, retry setting, pacing value, or
safety flag is hand-edited.

After a separate, current CEO authorization supplies the variable identity and
time-window values, execute with explicit output paths and the opt-in flag:

```sh
python -m src.football.top5_therundown_provider_native_discovery run \
  --quota-proof-package /absolute/path/to/current-b4-quota-proof.json \
  --discovery-authorization-id '<CEO-approved authorization ID>' \
  --ceo-discovery-authorization-identity '<CEO authorization identity>' \
  --issued-at '<RFC3339 UTC issue time>' \
  --expires-at '<RFC3339 UTC expiry time>' \
  --authorization-output /absolute/path/to/discovery-authorization-v3.json \
  --result-output /absolute/path/to/provider-native-discovery-v1.json \
  --execute-network
```

The credential is loaded only after canonical one-shot consumption, through the
existing protected `THERUNDOWN_API_KEY` reader. `--credential-file` may be used
only when the operator's existing protected credential file is at a different
absolute path; the credential itself is never an argument. Authorization and
result outputs are new, atomically created mode-0600 files. A failed or partial
run produces no success result artifact and cannot be retried under the same
authorization.

## Authorization contract

The executable structural path emits and accepts only
`top5-therundown-provider-native-discovery-authorization-v3` with
`selection_purpose=STRUCTURAL_PROVIDER`. Its five-league order is exactly
EPL, BL1, LL, SA, L1. It binds the current TheRundown adapter version and clean
repository HEAD, request-shape digest, exact current B4 quota-proof identity,
evidence/response digests, remaining headroom, account scope, snapshot,
timestamps, and reset time. The proof must retain at least 11,550 datapoints of
headroom under the current Discovery contract.

Structural Discovery has no lead-time fields. The current v2
`SIGNAL_TIME` authorization remains supported and continues to require its
explicit minimum and maximum lead values. Unsupported historical authorization
schemas remain invalid for execution; this path does not migrate or revive old
artifacts.

The current bounded Discovery contract remains code-owned: up to 21 snapshot
dates per league, 105 requests total, at most 55 datapoints per request, 5,775
datapoints total, at least 1.1 seconds between requests, and zero retries.
TheRundown is candidate-only; no authority, activation, publication, betting,
ledger mutation, or spend authority is emitted.

## Evidence path and B1 handoff

The successful native run is serialized with the production
`top5-therundown-provider-native-discovery-v1` schema. The existing B4 bridge
then derives, without synthesizing provider observations:

1. native authorization and provenance;
2. five native capture records and their projected five legacy Discovery
   evidence records;
3. the disabled prebound network configuration and its exact enabled execution
   projection;
4. the canonical five-league network-shadow result;
5. five-league reconciliation and qualification artifacts; and
6. the B4 typed evidence dossier.

The dossier retains the original validated B4 quota-proof package and the
canonical Shadow result in addition to normalized proof, headroom, native
provenance, five projected Discovery records, reconciliation, qualification,
and the current source SHA. Its
`Top5B4EvidenceDossierV1.b1_evidence_inputs(now=...)` method returns the exact
B4 fields the B1 bundle composer consumes:

- `source_main_sha`
- `b4_quota_proof_package` — original validated package, not reconstructed
- `b4_quota_headroom` — original validated typed evidence
- `discovery_evidence` — five canonical legacy projections
- `provider_native_discovery_provenance`
- `controlled_shadow` — canonical five-capture network result
- `b4_reconciliation` and `b4_qualification` with run/session/auth identities
- `b4_native_authorization` and `b4_dossier_digest`

B1 continues to supply its own model, runtime, and public-delivery inputs. It
must merge these B4 inputs into its real bundle; no synthetic reconstruction
of quota, Discovery, or Shadow evidence is part of this handoff. A successful
structural Discovery or Shadow still does not change provider authority,
activation, publication, betting, or ledger state.
