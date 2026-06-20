"""Tests für scripts/scrape_suspensions.py."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import scrape_suspensions as ss


# ── Helpers ──────────────────────────────────────────────────────


def _make_html(snippets: list[str]) -> str:
    body = "<html><body>" + " ".join(f"<p>{s}</p>" for s in snippets) + "</body></html>"
    return body


# ── _strip_html ──────────────────────────────────────────────────


def test_strip_html_removes_tags_and_scripts():
    html = "<p>Hello <b>World</b></p><script>alert('x')</script>"
    assert ss._strip_html(html) == "Hello World"


def test_strip_html_decodes_entities():
    html = "<p>Caf&amp;eacute; &amp; Tea</p>"
    out = ss._strip_html(html)
    assert "&" in out


# ── _windows_with_keyword ────────────────────────────────────────


def test_keyword_window_yields_only_matches():
    text = "No issue here. Player A was suspended for two games. Quiet again."
    windows = list(ss._windows_with_keyword(text))
    assert len(windows) == 1
    assert "suspended" in windows[0]


def test_keyword_window_catches_red_card():
    text = "Marcus Rashford received a red card and will miss the next match."
    windows = list(ss._windows_with_keyword(text))
    assert len(windows) >= 1


# ── _extract_names ───────────────────────────────────────────────


def test_extract_names_basic():
    snippet = "Marcus Rashford has been suspended after his red card."
    names = ss._extract_names(snippet)
    assert "Marcus Rashford" in names


def test_extract_names_filters_team_stopwords():
    snippet = "United States face a setback as FIFA Disciplinary panel suspends an unnamed player."
    names = ss._extract_names(snippet)
    assert "United States" not in names
    assert "FIFA Disciplinary" not in names


def test_extract_names_filters_months_weekdays():
    snippet = "Tuesday Morning saw the suspension confirmed before Lionel Messi spoke."
    names = ss._extract_names(snippet)
    assert "Lionel Messi" in names
    assert not any(n.startswith("Tuesday") for n in names)


# ── collect_candidates with mocked fetcher ───────────────────────


def test_collect_candidates_scores_single_source():
    src = ss.Source("FIFA", "https://fake/", 3)
    html = _make_html([
        "Lionel Messi has been suspended for the next World Cup match.",
    ])

    def fake_fetch(s):
        return html if s.name == "FIFA" else ""

    with patch.object(ss, "_load_known_squad_players", return_value={}):
        out = ss.collect_candidates([src], fetch=fake_fetch)

    assert "Lionel Messi" in out
    assert out["Lionel Messi"].score == 3
    assert out["Lionel Messi"].sources == {"FIFA"}


def test_collect_candidates_multi_source_bonus():
    srcs = [
        ss.Source("FIFA", "https://fake1/", 3),
        ss.Source("BBC",  "https://fake2/", 1),
    ]
    html = _make_html([
        "Vinicius Junior has been suspended for two games after a red card.",
    ])

    def fake_fetch(s):
        return html  # beide Quellen melden denselben Spieler

    with patch.object(ss, "_load_known_squad_players", return_value={}):
        out = ss.collect_candidates(srcs, fetch=fake_fetch)

    cand = out["Vinicius Junior"]
    # FIFA(3) + BBC(1) + Multi-Source-Bonus(2) = 6
    assert cand.score == 6
    assert cand.sources == {"FIFA", "BBC"}


def test_collect_candidates_squad_verified_bonus():
    src = ss.Source("FIFA", "https://fake/", 3)
    html = _make_html([
        "Harry Kane was sent off and will be suspended for the next match.",
    ])

    def fake_fetch(s):
        return html

    squad = {"Harry Kane": "England"}
    with patch.object(ss, "_load_known_squad_players", return_value=squad):
        out = ss.collect_candidates([src], fetch=fake_fetch)

    cand = out["Harry Kane"]
    # FIFA(3) + Squad-Verified(2) = 5
    assert cand.score == 5
    assert cand.team == "England"
    assert cand.squad_verified is True


def test_collect_candidates_skips_failed_fetch():
    src = ss.Source("UEFA", "https://fake/", 2)

    def fake_fetch(s):
        return ""  # HTTP-Fail

    out = ss.collect_candidates([src], fetch=fake_fetch)
    assert out == {}


# ── split_by_threshold ───────────────────────────────────────────


def test_split_requires_team_for_auto_merge():
    cands = {
        "X": ss.Candidate(player="X", team="Brazil", score=10),    # auto
        "Y": ss.Candidate(player="Y", team=None,    score=10),     # manual (kein Team)
        "Z": ss.Candidate(player="Z", team="Spain", score=3),      # manual (zu niedrig)
    }
    auto, manual = ss.split_by_threshold(cands, threshold=5)
    assert [c.player for c in auto] == ["X"]
    assert {c.player for c in manual} == {"Y", "Z"}


# ── merge_into_suspensions ───────────────────────────────────────


def test_merge_adds_only_new_players(tmp_path):
    f = tmp_path / "suspensions.json"
    f.write_text(json.dumps({"Brazil": ["Neymar"]}))
    with patch.object(ss, "_SUSPENSIONS_FILE", f):
        added = ss.merge_into_suspensions([
            ss.Candidate(player="Neymar",  team="Brazil", score=10),  # bereits drin
            ss.Candidate(player="Vinicius", team="Brazil", score=10),  # neu
        ])
    assert [c.player for c in added] == ["Vinicius"]
    data = json.loads(f.read_text())
    assert data["Brazil"] == ["Neymar", "Vinicius"]
    assert "_injuries_last_updated" in data


def test_merge_creates_team_entry(tmp_path):
    f = tmp_path / "suspensions.json"
    f.write_text(json.dumps({}))
    with patch.object(ss, "_SUSPENSIONS_FILE", f):
        added = ss.merge_into_suspensions([
            ss.Candidate(player="Lionel Messi", team="Argentina", score=10),
        ])
    assert len(added) == 1
    data = json.loads(f.read_text())
    assert data["Argentina"] == ["Lionel Messi"]


# ── persist_candidates ───────────────────────────────────────────


def test_persist_candidates_sorted_by_score(tmp_path):
    f = tmp_path / "candidates.json"
    with patch.object(ss, "_CANDIDATES_FILE", f):
        ss.persist_candidates([
            ss.Candidate(player="Low",  score=1, sources={"FIFA"}),
            ss.Candidate(player="High", score=4, sources={"BBC", "ESPN"}),
        ])
    payload = json.loads(f.read_text())
    names = [c["player"] for c in payload["candidates"]]
    assert names == ["High", "Low"]
