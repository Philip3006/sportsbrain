import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_rejects_pending_bets_below_value_threshold():
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import { pathToFileURL } from 'url';
        const worker = (await import(pathToFileURL('./cloudflare/worker.js').href)).default;
        const kv = {
          data: new Map(),
          async get(key) { return this.data.get(key) || null; },
          async put(key, value) { this.data.set(key, value); },
        };
        const env = { SIGNALS: kv, API_TOKEN: 'secret' };

        async function post(body) {
          return worker.fetch(new Request('https://worker.test/pending_bets', {
            method: 'POST',
            headers: {
              'Authorization': 'Bearer secret',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
          }), env);
        }

        const base = {
          match: 'Brazil vs Argentina',
          market: 'home',
          stake_eur: 5,
          model_prob: 0.50,
        };
        const bad = await post({ ...base, odds: 2.05 });
        if (bad.status !== 400) throw new Error(`expected reject, got ${bad.status}`);
        const good = await post({ ...base, odds: 2.10 });
        if (good.status !== 200) throw new Error(`expected accept, got ${good.status}: ${await good.text()}`);
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_rejects_goals_range_markets():
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import { pathToFileURL } from 'url';
        const worker = (await import(pathToFileURL('./cloudflare/worker.js').href)).default;
        const kv = {
          data: new Map(),
          async get(key) { return this.data.get(key) || null; },
          async put(key, value) { this.data.set(key, value); },
        };
        const env = { SIGNALS: kv, API_TOKEN: 'secret' };

        const response = await worker.fetch(new Request('https://worker.test/pending_bets', {
          method: 'POST',
          headers: {
            'Authorization': 'Bearer secret',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            match: 'England vs Croatia',
            market: 'h1_goals_2_4_no',
            odds: 1.80,
            stake_eur: 5,
            model_prob: 0.70,
          }),
        }), env);
        if (response.status !== 400) throw new Error(`expected reject, got ${response.status}`);
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_health_reads_signals_snapshot_and_automation_status():
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import { pathToFileURL } from 'url';
        const worker = (await import(pathToFileURL('./cloudflare/worker.js').href)).default;
        const kv = {
          data: new Map([
            ['signals_json', JSON.stringify({
              updated: '2026-06-18T08:00:00Z',
              system_health: { status: 'warn', open_bets: 1 },
              data_freshness: { signals_stale: true },
              model_status: { status: 'ok' },
              alerts: [{ level: 'warn', code: 'STALE_SCAN', message: 'stale' }],
            })],
            ['automation_status', JSON.stringify({ status: 'ok', job: 'scan' })],
          ]),
          async get(key) { return this.data.get(key) || null; },
          async put(key, value) { this.data.set(key, value); },
        };
        const env = { SIGNALS: kv, API_TOKEN: 'secret' };

        const response = await worker.fetch(new Request('https://worker.test/health'), env);
        if (response.status !== 200) throw new Error(`expected 200, got ${response.status}`);
        const body = await response.json();
        if (body.status !== 'warn') throw new Error(`unexpected status ${body.status}`);
        if (body.alerts[0].code !== 'STALE_SCAN') throw new Error('missing alert');
        if (body.automation.job !== 'scan') throw new Error('missing automation status');
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_automation_status_requires_auth_and_valid_payload():
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import { pathToFileURL } from 'url';
        const worker = (await import(pathToFileURL('./cloudflare/worker.js').href)).default;
        const kv = {
          data: new Map(),
          async get(key) { return this.data.get(key) || null; },
          async put(key, value) { this.data.set(key, value); },
        };
        const env = { SIGNALS: kv, API_TOKEN: 'secret' };

        const unauth = await worker.fetch(new Request('https://worker.test/automation_status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'ok', job: 'scan' }),
        }), env);
        if (unauth.status !== 401) throw new Error(`expected 401, got ${unauth.status}`);

        const invalid = await worker.fetch(new Request('https://worker.test/automation_status', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer secret', 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'maybe', job: 'scan' }),
        }), env);
        if (invalid.status !== 400) throw new Error(`expected 400, got ${invalid.status}`);

        const valid = await worker.fetch(new Request('https://worker.test/automation_status', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer secret', 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'ok', job: 'scan', message: 'done' }),
        }), env);
        if (valid.status !== 200) throw new Error(`expected 200, got ${valid.status}`);
        const stored = JSON.parse(kv.data.get('automation_status'));
        if (stored.status !== 'ok' || stored.job !== 'scan' || !stored.received_at) {
          throw new Error('automation status not stored');
        }
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
