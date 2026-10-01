from __future__ import annotations

import hashlib
import json
import math
import random
import struct
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from src.analysis.nations_league_live_edge import (
    build_edge_analysis,
    build_market_snapshot,
)
from src.notifications.nations_league_live_public import (
    _public_canonical,
    _public_digest,
    build_live_public_nations_league,
    validate_live_public_nations_league,
)
from tests.notifications.test_nations_league_live_public import (
    _active_release,
    _binding,
    _records,
)

ROOT = Path(__file__).parents[2]


def _finite_binary64_samples(count: int = 100_000) -> list[float]:
    """Return a deterministic broad finite IEEE-754 binary64 sample."""

    boundary_values = [
        0.0,
        -0.0,
        1e-7,
        math.nextafter(1e-6, 0.0),
        1e-6,
        math.nextafter(1e-6, math.inf),
        1e-5,
        -1e-7,
        -math.nextafter(1e-6, 0.0),
        -1e-6,
        -math.nextafter(1e-6, math.inf),
        -1e-5,
        1e20,
        math.nextafter(1e21, 0.0),
        1e21,
        math.nextafter(1e21, math.inf),
        1e22,
        -1e20,
        -math.nextafter(1e21, 0.0),
        -1e21,
        -math.nextafter(1e21, math.inf),
        -1e22,
        math.ldexp(1.0, -1074),
        -math.ldexp(1.0, -1074),
        math.nextafter(sys.float_info.min, 0.0),
        -math.nextafter(sys.float_info.min, 0.0),
        sys.float_info.min,
        -sys.float_info.min,
        sys.float_info.max,
        -sys.float_info.max,
    ]
    values = list(boundary_values)
    rng = random.Random(0x4E4C5F323531)
    while len(values) < count:
        bits = rng.getrandbits(64)
        value = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
        if math.isfinite(value):
            values.append(value)
    return values


def _node_canonicalizes(path: Path) -> str:
    script = """
import { readFileSync } from 'node:fs';

const input = JSON.parse(readFileSync(process.argv[1], 'utf8'));
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${canonical(value[key])}`
    ).join(',')}}`;
  }
  return JSON.stringify(value);
}
process.stdout.write(canonical(input));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _node_accepts_digest(path: Path, *, expected: bool) -> None:
    worker = (ROOT / "cloudflare/worker.js").as_posix()
    app = (ROOT / "docs/js/app.js").as_posix()
    script = f"""
import {{ readFileSync }} from 'node:fs';
import {{ pathToFileURL }} from 'node:url';
import vm from 'node:vm';
import {{ webcrypto }} from 'node:crypto';

const payload = JSON.parse(readFileSync(process.argv[1], 'utf8')).nations_league;
const worker = await import(pathToFileURL({json.dumps(worker)}).href);
let workerAccepted = false;
try {{
  await worker.validatePublicNationsLeagueDigest(payload);
  workerAccepted = true;
}} catch {{}}

const appSource = readFileSync({json.dumps(app)}, 'utf8');
const start = appSource.indexOf('function _canonicalNationsLeagueJson(');
const end = appSource.indexOf('\\nfunction _top5LifecycleError', start);
if (start < 0 || end < 0) throw new Error('PWA digest validator seam missing');
const context = {{ crypto: webcrypto, TextEncoder, JSON, Object, Array, Uint8Array, String, Date, Number, Math }};
vm.createContext(context);
vm.runInContext(appSource.slice(start, end) +
  '\\nglobalThis.digest = _validNationsLeaguePublicDigest;', context);
const pwaAccepted = await context.digest(payload);
const expected = {str(expected).lower()};
if (workerAccepted !== expected || pwaAccepted !== expected) {{
  throw new Error(`digest parity mismatch: worker=${{workerAccepted}} pwa=${{pwaAccepted}} expected=${{expected}}`);
}}
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script, str(path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _public_bundle_with_edge() -> dict:
    records = deepcopy(_records())
    release = _active_release()
    source = records[0]
    snapshot = build_market_snapshot(
        {
            "provider": "isports_api",
            "bookmaker": "Research bookmaker median",
            "captured_at": source["prediction_timestamp"],
            "fixture_id": source["fixture_id"],
            "odds_decimal": {"home": 2.3, "draw": 3.2, "away": 3.0},
        }
    )
    source["edge_analysis"] = build_edge_analysis(
        source["probabilities"],
        fixture_id=source["fixture_id"],
        phase=source["phase"],
        model_release_id=release.release_id,
        prediction_record_id=source["record_id"],
        prediction_timestamp=source["prediction_timestamp"],
        market_snapshots=[snapshot],
    )
    return build_live_public_nations_league(
        records,
        active_release=release,
        evidence_binding=_binding(),
        as_of="2026-09-30T20:01:24Z",
    )


def test_public_canonical_matches_native_json_stringify_for_numeric_matrix_and_edge_fields(
    tmp_path,
):
    source = _records()[0]
    snapshot = build_market_snapshot(
        {
            "provider": "isports_api",
            "bookmaker": "Research bookmaker median",
            "captured_at": source["prediction_timestamp"],
            "fixture_id": source["fixture_id"],
            "odds_decimal": {"home": 2.3, "draw": 3.2, "away": 3.0},
        }
    )
    edge = build_edge_analysis(
        source["probabilities"],
        fixture_id=source["fixture_id"],
        phase=source["phase"],
        model_release_id=_active_release().release_id,
        prediction_record_id=source["record_id"],
        prediction_timestamp=source["prediction_timestamp"],
        market_snapshots=[snapshot],
    )
    actual_nl_fields = {
        "odds_decimal": snapshot["odds_decimal"],
        "overround": snapshot["overround"],
        "margin_free_probabilities": snapshot["margin_free_probabilities"],
        "probability_edge": {
            key: value["probability_edge"] for key, value in edge["outcomes"].items()
        },
        "ev": {key: value["ev"] for key, value in edge["outcomes"].items()},
        "model_probabilities": source["probabilities"],
    }
    payload = {
        "numbers": [
            0,
            -0.0,
            3.0,
            1.5,
            0.000001,
            0.0000001,
            0.00001,
            999999.999999,
            100000000000000000000,
            1e21,
            1.234567,
            -1.234567,
            1.2e-7,
            -1.2e-7,
        ],
        "nations_league": actual_nl_fields,
    }
    path = tmp_path / "canonical-input.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert _public_canonical(payload) == _node_canonicalizes(path)


def test_public_canonical_matches_native_json_stringify_for_100000_binary64_values(
    tmp_path,
):
    values = _finite_binary64_samples()
    assert len(values) == 100_000
    assert any(value > 0 for value in values)
    assert any(value < 0 for value in values)
    assert any(0 < abs(value) < sys.float_info.min for value in values)
    assert any(abs(value) > 1e300 for value in values)

    payload = {"values": values}
    path = tmp_path / "binary64-input.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )

    # The Node helper sorts object keys only; every numeric token is emitted by
    # native JSON.stringify, which remains the reference implementation.
    assert _public_canonical(payload) == _node_canonicalizes(path)


def test_python_digest_is_accepted_by_worker_and_pwa_and_numeric_mutation_is_rejected(
    tmp_path,
):
    public = _public_bundle_with_edge()
    assert validate_live_public_nations_league(public) == public
    path = tmp_path / "signals.json"

    def write_and_check(expected: bool) -> None:
        path.write_text(
            json.dumps({"nations_league": public}, ensure_ascii=False), encoding="utf-8"
        )
        _node_accepts_digest(path, expected=expected)

    write_and_check(True)

    edged_fixture = next(
        fixture
        for fixture in public["fixtures"]
        if fixture["edge_analysis"]["outcomes"]
    )
    edge = edged_fixture["edge_analysis"]
    home = edge["outcomes"]["home"]
    home["decimal_odds"] += 0.000001
    home["ev"] = round(home["model_probability"] * home["decimal_odds"] - 1.0, 6)
    edge["edge_digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in edge.items() if key != "edge_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    write_and_check(False)

    body = {key: value for key, value in public.items() if key != "public_digest"}
    public["public_digest"] = _public_digest(body)
    assert validate_live_public_nations_league(public) == public
    write_and_check(True)
