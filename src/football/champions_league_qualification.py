"""Compatibility facade for the offline Champions League evidence contract.

The implementation lives in :mod:`champions_league_candidate_evidence` so
callers can discover the contract by either its qualification or evidence
name.  Both entry points remain offline and validation-only.
"""

from src.football.champions_league_candidate_evidence import *  # noqa: F401,F403
from src.football.champions_league_candidate_evidence import __all__

