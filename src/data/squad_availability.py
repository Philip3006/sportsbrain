"""
Compatibility shim — re-exports the full public API from the split squad_* modules.
All existing imports from src.data.squad_availability continue to work unchanged.
"""
from __future__ import annotations

from .squad_models import (
    AVAILABILITY,
    _TM_TEAMS,
    _CACHE_DIR, _CACHE_TTL_HOURS,
    _SUSPENSIONS_FILE,
    _UA, _HEADERS,
    _POS_MAP, _RETURN_RE, _MV_RE,
    _parse_market_value_m,
    PlayerStatus,
    SquadReport,
    default_report,
    _cache_path, _wiki_cache_path,
    _cache_fresh, _save_cache, _load_cache,
)
from .squad_covers import _fetch_covers_squad, _overlay_sofascore_values
from .squad_wikipedia import (
    _WC_SECTION_MAP, _WC_PAGE, _WIKI_API,
    _WT_NAME_RE, _WT_POS_RE,
    _WIKI_POS_MAP, _WIKI_SLUG_OVERRIDES,
    _parse_wikitext_squad,
    _fetch_wc_squads_page,
    _fetch_wikipedia_squad,
    _parse_wikipedia_squad_html,
)
from .squad_transfermarkt import (
    fetch_transfermarkt_squad,
    _scrape_kader_playwright,
    _parse_return_date,
    _map_position,
    fetch_fbref_player_form,
)
from .squad_merger import (
    load_suspensions,
    get_suspended_players,
    _apply_suspension_overlay,
    _apply_suspension_overlay_to_statuses,
    squad_report,
    _POSITION_IMPACT_WEIGHTS,
    _weighted_impact_lost,
    squad_impact_features,
)

__all__ = [
    "AVAILABILITY",
    "_TM_TEAMS",
    "_CACHE_DIR", "_CACHE_TTL_HOURS",
    "_SUSPENSIONS_FILE",
    "_UA", "_HEADERS",
    "_POS_MAP", "_RETURN_RE", "_MV_RE",
    "_parse_market_value_m",
    "PlayerStatus", "SquadReport",
    "default_report",
    "_cache_path", "_wiki_cache_path",
    "_cache_fresh", "_save_cache", "_load_cache",
    "_fetch_covers_squad", "_overlay_sofascore_values",
    "_WC_SECTION_MAP", "_WC_PAGE", "_WIKI_API",
    "_WT_NAME_RE", "_WT_POS_RE",
    "_WIKI_POS_MAP", "_WIKI_SLUG_OVERRIDES",
    "_parse_wikitext_squad",
    "_fetch_wc_squads_page",
    "_fetch_wikipedia_squad",
    "_parse_wikipedia_squad_html",
    "fetch_transfermarkt_squad",
    "_scrape_kader_playwright",
    "_parse_return_date",
    "_map_position",
    "fetch_fbref_player_form",
    "load_suspensions",
    "get_suspended_players",
    "_apply_suspension_overlay",
    "_apply_suspension_overlay_to_statuses",
    "squad_report",
    "_POSITION_IMPACT_WEIGHTS",
    "_weighted_impact_lost",
    "squad_impact_features",
]
