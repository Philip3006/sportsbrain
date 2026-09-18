# Offline post-merge verification

`scripts/post_merge_verifier.py` is a read-only, offline verifier for the
Top-5 integration train. It never merges, commits, pushes, resets, rebases,
deploys, or calls a provider.

It archives the selected `HEAD` into a temporary directory, blocks sockets in
the child process, compiles/imports the repository, and runs the relevant
provider, bridge, qualification, quota, and shadow tests without writing test
artifacts into the working checkout.

## Train steps

Run after each merge, with the pre-merge `main` SHA as the baseline:

```sh
python3 scripts/post_merge_verifier.py \
  --step 88 \
  --baseline-sha PRE_MERGE_MAIN_SHA \
  --format json
```

Use `91`, `92`, `94`, or `95` for later train steps. The verifier uses the
fixed order `88 → 91 → 92 → 94 → 95`; health, runtime, result, cache, and
other volatile data do not select or reorder steps.

For a later receipt or shadow follow-up:

```sh
python3 scripts/post_merge_verifier.py \
  --step follow-up \
  --baseline-sha PRE_MERGE_MAIN_SHA \
  --required-path src/football/example_follow_up.py \
  --test-path tests/football/test_example_follow_up.py
```

## Verification contract

The verifier checks:

- required contract paths and exactly-one definitions of expected symbols;
- repository compilation and imports;
- relevant deterministic test suites under a socket-blocked subprocess;
- The Odds API as the only active Football authority;
- TheRundown as candidate/shadow-only;
- `fetch_observations()` as the qualification path;
- separate authority, activation, and publication concepts;
- disabled publication, betting, scheduler, ledger, and Cloudflare paths;
- absence of a duplicate PR #93 implementation;
- fixed train ordering independent of health/runtime drift.

The command exits `0` only when every check passes.
