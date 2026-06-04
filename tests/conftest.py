import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from src.models.dixon_coles import DixonColesParams


def _generate_synthetic_matches(
    teams: list[str],
    attack: dict[str, float],
    defence: dict[str, float],
    home_adv: float,
    rho: float,
    n_matches: int = 600,
    seed: int = 42,
) -> pd.DataFrame:
    """Generates synthetic match data from known Dixon-Coles parameters."""
    rng = np.random.default_rng(seed)
    rows = []
    base_date = pd.Timestamp("2020-01-01")
    pairs = [(h, a) for h in teams for a in teams if h != a]
    for i in range(n_matches):
        h, a = pairs[i % len(pairs)]
        lh = np.exp(attack[h] + defence[a] + home_adv)
        la = np.exp(attack[a] + defence[h])
        hg = int(rng.poisson(lh))
        ag = int(rng.poisson(la))
        rows.append({
            "date": base_date + pd.Timedelta(days=i),
            "home_team": h,
            "away_team": a,
            "home_score": hg,
            "away_score": ag,
            "tournament": "FIFA World Cup",
            "neutral": False,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def synthetic_teams():
    return ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]


@pytest.fixture
def synthetic_params(synthetic_teams):
    rng = np.random.default_rng(0)
    attack = {t: rng.uniform(-0.5, 0.5) for t in synthetic_teams}
    attack[synthetic_teams[0]] = 0.0  # reference team pinned
    defence = {t: rng.uniform(-0.5, 0.5) for t in synthetic_teams}
    return {
        "attack": attack,
        "defence": defence,
        "home_adv": 0.25,
        "rho": -0.13,
    }


@pytest.fixture
def synthetic_matches(synthetic_teams, synthetic_params):
    return _generate_synthetic_matches(
        teams=synthetic_teams,
        n_matches=600,
        **synthetic_params,
    )


@pytest.fixture
def fitted_params(synthetic_matches):
    from src.models.dixon_coles import fit
    return fit(synthetic_matches, max_iter=500)


@pytest.fixture
def minimal_dc_params():
    return DixonColesParams(
        attack={"Home": 0.3, "Away": -0.1},
        defence={"Home": -0.2, "Away": 0.1},
        home_adv=0.25,
        rho=-0.13,
        fit_date=pd.Timestamp("2024-01-01"),
    )
