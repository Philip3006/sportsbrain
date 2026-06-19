"""Health-monitoring for SportsBrain cron jobs.

Each cron wrapper writes a status JSON to results/health/{job}.json on
every run. aggregate_health.py reads all of them and produces the
consolidated docs/data/health.json that the dashboard polls.

Status values:
    ok        — job finished cleanly within the expected window
    degraded  — job finished, but used a fallback data source
    error     — job failed (non-zero exit) on its most recent run
    stale     — last run is older than the expected next-run window
"""
