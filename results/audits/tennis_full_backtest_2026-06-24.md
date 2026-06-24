# Tennis Full Backtest — 2026-06-24

Generiert von `scripts/tennis_full_backtest.py`. Datenbasis: tennis-data.co.uk Full-Tour-XLSX (Match-Outcomes + B365/Avg/Max-Odds + per-Set-Game-Scores). Elo wird walk-forward aus denselben XLSX-Daten aufgebaut (Sackmann-Repos sind ab 2026-06 nicht mehr öffentlich verfügbar).

## 1. Match Winner (ROI-validiert)

Live-Gate: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30. BLACKLIST: ROI≤-5%.

| Kategorie | Tour | Surface | N | Hit% | ROI | Brier | Verdict |
|---|---|---|---:|---:|---:|---:|---|
| atp250 | ATP | clay | 676 | 41.1% | +3.8% | 0.2515 | ✅ LIVE |
| atp250 | ATP | grass | 234 | 37.6% | -2.6% | 0.2477 | ⚠️ SHADOW |
| atp250 | ATP | hard | 914 | 43.2% | +5.3% | 0.2472 | ✅ LIVE |
| atp500 | ATP | clay | 127 | 40.9% | +0.8% | 0.2450 | ⚠️ SHADOW |
| atp500 | ATP | grass | 112 | 43.8% | +18.6% | 0.2506 | ✅ LIVE |
| atp500 | ATP | hard | 398 | 41.2% | -8.8% | 0.2401 | 🚫 BLACKLIST |
| grand_slam | ATP | grass | 200 | 35.5% | -9.5% | 0.2473 | 🚫 BLACKLIST |
| grand_slam | WTA | grass | 242 | 40.1% | -0.7% | 0.2414 | ⚠️ SHADOW |
| m1000 | ATP | clay | 410 | 37.8% | -9.4% | 0.2436 | 🚫 BLACKLIST |
| m1000 | ATP | hard | 728 | 45.6% | +0.4% | 0.2436 | ⚠️ SHADOW |
| tour_final | ATP | hard | 29 | 58.6% | +40.2% | 0.2498 | ⚠️ SHADOW |
| tour_final | WTA | hard | 43 | 37.2% | -13.0% | 0.2681 | 🚫 BLACKLIST |
| wta1000 | WTA | clay | 382 | 45.3% | +8.4% | 0.2439 | ✅ LIVE |
| wta1000 | WTA | hard | 1058 | 41.6% | -5.3% | 0.2470 | 🚫 BLACKLIST |
| wta250 | WTA | clay | 486 | 41.8% | +1.1% | 0.2422 | ⚠️ SHADOW |
| wta250 | WTA | grass | 258 | 45.0% | +16.0% | 0.2514 | ✅ LIVE |
| wta250 | WTA | hard | 921 | 40.4% | -2.4% | 0.2479 | ⚠️ SHADOW |
| wta500 | WTA | clay | 182 | 40.7% | -1.0% | 0.2454 | ⚠️ SHADOW |
| wta500 | WTA | grass | 147 | 40.8% | +8.1% | 0.2498 | ✅ LIVE |
| wta500 | WTA | hard | 681 | 42.0% | -4.4% | 0.2414 | ⚠️ SHADOW |

## 2. Set-Märkte Kalibrierung (Brier, keine ROI — keine historischen Quoten)

Kalibriert: Brier < 0.245

| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |
|---|---|---|---|---:|---:|---:|---|
| atp250 | ATP | clay | o_u_sets_2.5_over | 2344 | 38.9% | 0.2471 | ⚠️ |
| atp250 | ATP | clay | score_0-2 | 2344 | — | 0.0621 | ✅ |
| atp250 | ATP | clay | score_1-1 | 11 | — | 1.0000 | ⚠️ |
| atp250 | ATP | clay | score_1-2 | 2344 | — | 0.0576 | ✅ |
| atp250 | ATP | clay | score_2-0 | 2344 | — | 0.3500 | ⚠️ |
| atp250 | ATP | clay | score_2-1 | 2344 | — | 0.2595 | ⚠️ |
| atp250 | ATP | grass | o_u_sets_2.5_over | 737 | 35.7% | 0.2482 | ⚠️ |
| atp250 | ATP | grass | score_0-2 | 737 | — | 0.0611 | ✅ |
| atp250 | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| atp250 | ATP | grass | score_1-2 | 737 | — | 0.0589 | ✅ |
| atp250 | ATP | grass | score_2-0 | 737 | — | 0.3668 | ⚠️ |
| atp250 | ATP | grass | score_2-1 | 737 | — | 0.2441 | ✅ |
| atp250 | ATP | hard | o_u_sets_2.5_over | 3635 | 35.6% | 0.2457 | ⚠️ |
| atp250 | ATP | hard | score_0-2 | 3635 | — | 0.0612 | ✅ |
| atp250 | ATP | hard | score_1-1 | 27 | — | 1.0000 | ⚠️ |
| atp250 | ATP | hard | score_1-2 | 3635 | — | 0.0560 | ✅ |
| atp250 | ATP | hard | score_2-0 | 3635 | — | 0.3575 | ⚠️ |
| atp250 | ATP | hard | score_2-1 | 3635 | — | 0.2443 | ✅ |
| atp500 | ATP | clay | o_u_sets_2.5_over | 550 | 35.1% | 0.2432 | ✅ |
| atp500 | ATP | clay | score_0-2 | 550 | — | 0.0579 | ✅ |
| atp500 | ATP | clay | score_1-1 | 1 | — | 1.0000 | ⚠️ |
| atp500 | ATP | clay | score_1-2 | 550 | — | 0.0535 | ✅ |
| atp500 | ATP | clay | score_2-0 | 550 | — | 0.3543 | ⚠️ |
| atp500 | ATP | clay | score_2-1 | 550 | — | 0.2405 | ✅ |
| atp500 | ATP | grass | o_u_sets_2.5_over | 363 | 38.8% | 0.2465 | ⚠️ |
| atp500 | ATP | grass | score_0-2 | 363 | — | 0.0565 | ✅ |
| atp500 | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| atp500 | ATP | grass | score_1-2 | 363 | — | 0.0535 | ✅ |
| atp500 | ATP | grass | score_2-0 | 363 | — | 0.3366 | ⚠️ |
| atp500 | ATP | grass | score_2-1 | 363 | — | 0.2587 | ⚠️ |
| atp500 | ATP | hard | o_u_sets_2.5_over | 1630 | 35.3% | 0.2392 | ✅ |
| atp500 | ATP | hard | score_0-2 | 1630 | — | 0.0585 | ✅ |
| atp500 | ATP | hard | score_1-1 | 12 | — | 1.0000 | ⚠️ |
| atp500 | ATP | hard | score_1-2 | 1630 | — | 0.0491 | ✅ |
| atp500 | ATP | hard | score_2-0 | 1630 | — | 0.3344 | ⚠️ |
| atp500 | ATP | hard | score_2-1 | 1630 | — | 0.2439 | ✅ |
| grand_slam | ATP | grass | o_u_sets_2.5_over | 6 | 100.0% | 0.2856 | ⚠️ |
| grand_slam | ATP | grass | o_u_sets_3.5_over | 745 | 53.6% | 0.2861 | ⚠️ |
| grand_slam | ATP | grass | score_0-2 | 7 | — | 0.1786 | ✅ |
| grand_slam | ATP | grass | score_0-3 | 745 | — | 0.0132 | ✅ |
| grand_slam | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_1-2 | 6 | — | 0.0436 | ✅ |
| grand_slam | ATP | grass | score_1-3 | 745 | — | 0.0286 | ✅ |
| grand_slam | ATP | grass | score_2-0 | 11 | — | 0.5276 | ⚠️ |
| grand_slam | ATP | grass | score_2-1 | 8 | — | 0.3041 | ⚠️ |
| grand_slam | ATP | grass | score_2-2 | 2 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_2-3 | 745 | — | 0.0291 | ✅ |
| grand_slam | ATP | grass | score_3-0 | 749 | — | 0.3275 | ⚠️ |
| grand_slam | ATP | grass | score_3-1 | 745 | — | 0.2395 | ✅ |
| grand_slam | ATP | grass | score_3-2 | 747 | — | 0.1653 | ✅ |
| grand_slam | WTA | grass | o_u_sets_2.5_over | 746 | 31.4% | 0.2449 | ✅ |
| grand_slam | WTA | grass | score_0-2 | 746 | — | 0.0547 | ✅ |
| grand_slam | WTA | grass | score_1-1 | 4 | — | 1.0000 | ⚠️ |
| grand_slam | WTA | grass | score_1-2 | 746 | — | 0.0540 | ✅ |
| grand_slam | WTA | grass | score_2-0 | 746 | — | 0.3643 | ⚠️ |
| grand_slam | WTA | grass | score_2-1 | 746 | — | 0.2244 | ✅ |
| m1000 | ATP | clay | o_u_sets_2.5_over | 1243 | 36.6% | 0.2429 | ✅ |
| m1000 | ATP | clay | score_0-2 | 1243 | — | 0.0596 | ✅ |
| m1000 | ATP | clay | score_1-1 | 6 | — | 1.0000 | ⚠️ |
| m1000 | ATP | clay | score_1-2 | 1243 | — | 0.0530 | ✅ |
| m1000 | ATP | clay | score_2-0 | 1243 | — | 0.3452 | ⚠️ |
| m1000 | ATP | clay | score_2-1 | 1243 | — | 0.2488 | ⚠️ |
| m1000 | ATP | hard | o_u_sets_2.5_over | 2539 | 35.7% | 0.2412 | ✅ |
| m1000 | ATP | hard | score_0-2 | 2539 | — | 0.0611 | ✅ |
| m1000 | ATP | hard | score_1-1 | 9 | — | 1.0000 | ⚠️ |
| m1000 | ATP | hard | score_1-2 | 2539 | — | 0.0509 | ✅ |
| m1000 | ATP | hard | score_2-0 | 2539 | — | 0.3377 | ⚠️ |
| m1000 | ATP | hard | score_2-1 | 2539 | — | 0.2481 | ⚠️ |
| tour_final | ATP | hard | o_u_sets_2.5_over | 103 | 34.0% | 0.2332 | ✅ |
| tour_final | ATP | hard | score_0-2 | 103 | — | 0.0561 | ✅ |
| tour_final | ATP | hard | score_1-2 | 103 | — | 0.0475 | ✅ |
| tour_final | ATP | hard | score_2-0 | 103 | — | 0.3262 | ⚠️ |
| tour_final | ATP | hard | score_2-1 | 103 | — | 0.2397 | ✅ |
| tour_final | WTA | hard | o_u_sets_2.5_over | 118 | 36.4% | 0.2455 | ⚠️ |
| tour_final | WTA | hard | score_0-2 | 118 | — | 0.0622 | ✅ |
| tour_final | WTA | hard | score_1-1 | 1 | — | 1.0000 | ⚠️ |
| tour_final | WTA | hard | score_1-2 | 118 | — | 0.0556 | ✅ |
| tour_final | WTA | hard | score_2-0 | 118 | — | 0.3572 | ⚠️ |
| tour_final | WTA | hard | score_2-1 | 118 | — | 0.2467 | ⚠️ |
| wta1000 | WTA | clay | o_u_sets_2.5_over | 775 | 35.1% | 0.2436 | ✅ |
| wta1000 | WTA | clay | score_0-2 | 775 | — | 0.0548 | ✅ |
| wta1000 | WTA | clay | score_1-1 | 2 | — | 1.0000 | ⚠️ |
| wta1000 | WTA | clay | score_1-2 | 775 | — | 0.0536 | ✅ |
| wta1000 | WTA | clay | score_2-0 | 775 | — | 0.3505 | ⚠️ |
| wta1000 | WTA | clay | score_2-1 | 775 | — | 0.2392 | ✅ |
| wta1000 | WTA | hard | o_u_sets_2.5_over | 2200 | 35.9% | 0.2423 | ✅ |
| wta1000 | WTA | hard | score_0-2 | 2200 | — | 0.0604 | ✅ |
| wta1000 | WTA | hard | score_1-1 | 13 | — | 1.0000 | ⚠️ |
| wta1000 | WTA | hard | score_1-2 | 2200 | — | 0.0518 | ✅ |
| wta1000 | WTA | hard | score_2-0 | 2200 | — | 0.3400 | ⚠️ |
| wta1000 | WTA | hard | score_2-1 | 2200 | — | 0.2485 | ⚠️ |
| wta250 | WTA | clay | o_u_sets_2.5_over | 1765 | 35.1% | 0.2483 | ⚠️ |
| wta250 | WTA | clay | score_0-2 | 1765 | — | 0.0604 | ✅ |
| wta250 | WTA | clay | score_1-1 | 15 | — | 1.0000 | ⚠️ |
| wta250 | WTA | clay | score_1-2 | 1765 | — | 0.0593 | ✅ |
| wta250 | WTA | clay | score_2-0 | 1765 | — | 0.3711 | ⚠️ |
| wta250 | WTA | clay | score_2-1 | 1765 | — | 0.2390 | ✅ |
| wta250 | WTA | grass | o_u_sets_2.5_over | 667 | 35.8% | 0.2482 | ⚠️ |
| wta250 | WTA | grass | score_0-2 | 667 | — | 0.0589 | ✅ |
| wta250 | WTA | grass | score_1-1 | 4 | — | 1.0000 | ⚠️ |
| wta250 | WTA | grass | score_1-2 | 667 | — | 0.0582 | ✅ |
| wta250 | WTA | grass | score_2-0 | 667 | — | 0.3622 | ⚠️ |
| wta250 | WTA | grass | score_2-1 | 667 | — | 0.2445 | ✅ |
| wta250 | WTA | hard | o_u_sets_2.5_over | 2945 | 35.1% | 0.2470 | ⚠️ |
| wta250 | WTA | hard | score_0-2 | 2945 | — | 0.0603 | ✅ |
| wta250 | WTA | hard | score_1-1 | 24 | — | 1.0000 | ⚠️ |
| wta250 | WTA | hard | score_1-2 | 2945 | — | 0.0577 | ✅ |
| wta250 | WTA | hard | score_2-0 | 2945 | — | 0.3648 | ⚠️ |
| wta250 | WTA | hard | score_2-1 | 2945 | — | 0.2404 | ✅ |
| wta500 | WTA | clay | o_u_sets_2.5_over | 673 | 33.0% | 0.2462 | ⚠️ |
| wta500 | WTA | clay | score_0-2 | 673 | — | 0.0581 | ✅ |
| wta500 | WTA | clay | score_1-1 | 8 | — | 1.0000 | ⚠️ |
| wta500 | WTA | clay | score_1-2 | 673 | — | 0.0567 | ✅ |
| wta500 | WTA | clay | score_2-0 | 673 | — | 0.3689 | ⚠️ |
| wta500 | WTA | clay | score_2-1 | 673 | — | 0.2297 | ✅ |
| wta500 | WTA | grass | o_u_sets_2.5_over | 362 | 37.0% | 0.2462 | ⚠️ |
| wta500 | WTA | grass | score_0-2 | 362 | — | 0.0614 | ✅ |
| wta500 | WTA | grass | score_1-1 | 2 | — | 1.0000 | ⚠️ |
| wta500 | WTA | grass | score_1-2 | 362 | — | 0.0578 | ✅ |
| wta500 | WTA | grass | score_2-0 | 362 | — | 0.3536 | ⚠️ |
| wta500 | WTA | grass | score_2-1 | 362 | — | 0.2518 | ⚠️ |
| wta500 | WTA | hard | o_u_sets_2.5_over | 2301 | 34.4% | 0.2455 | ⚠️ |
| wta500 | WTA | hard | score_0-2 | 2301 | — | 0.0584 | ✅ |
| wta500 | WTA | hard | score_1-1 | 10 | — | 1.0000 | ⚠️ |
| wta500 | WTA | hard | score_1-2 | 2301 | — | 0.0545 | ✅ |
| wta500 | WTA | hard | score_2-0 | 2301 | — | 0.3588 | ⚠️ |
| wta500 | WTA | hard | score_2-1 | 2301 | — | 0.2385 | ✅ |

## 3. Game-Märkte Kalibrierung (Brier, MC-Sim)

| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |
|---|---|---|---|---:|---:|---:|---|
| atp250 | ATP | clay | o_u_games_21.5_over | 2344 | 55.3% | 0.2634 | ⚠️ |
| atp250 | ATP | grass | o_u_games_21.5_over | 737 | 62.6% | 0.2384 | ✅ |
| atp250 | ATP | hard | o_u_games_21.5_over | 3635 | 55.1% | 0.2629 | ⚠️ |
| atp500 | ATP | clay | o_u_games_21.5_over | 550 | 51.3% | 0.2759 | ⚠️ |
| atp500 | ATP | grass | o_u_games_21.5_over | 363 | 64.5% | 0.2316 | ✅ |
| atp500 | ATP | hard | o_u_games_21.5_over | 1630 | 54.7% | 0.2582 | ⚠️ |
| grand_slam | ATP | grass | o_u_games_21.5_over | 6 | 100.0% | 0.1060 | ✅ |
| grand_slam | ATP | grass | o_u_games_38.5_over | 745 | 42.8% | 0.2837 | ⚠️ |
| grand_slam | WTA | grass | o_u_games_21.5_over | 746 | 41.4% | 0.3024 | ⚠️ |
| m1000 | ATP | clay | o_u_games_21.5_over | 1243 | 52.9% | 0.2681 | ⚠️ |
| m1000 | ATP | hard | o_u_games_21.5_over | 2539 | 55.6% | 0.2567 | ⚠️ |
| tour_final | ATP | hard | o_u_games_21.5_over | 103 | 53.4% | 0.2580 | ⚠️ |
| tour_final | WTA | hard | o_u_games_21.5_over | 118 | 49.2% | 0.2777 | ⚠️ |
| wta1000 | WTA | clay | o_u_games_21.5_over | 775 | 44.1% | 0.2925 | ⚠️ |
| wta1000 | WTA | hard | o_u_games_21.5_over | 2200 | 46.4% | 0.2831 | ⚠️ |
| wta250 | WTA | clay | o_u_games_21.5_over | 1765 | 45.5% | 0.2901 | ⚠️ |
| wta250 | WTA | grass | o_u_games_21.5_over | 667 | 48.4% | 0.2801 | ⚠️ |
| wta250 | WTA | hard | o_u_games_21.5_over | 2945 | 45.4% | 0.2902 | ⚠️ |
| wta500 | WTA | clay | o_u_games_21.5_over | 673 | 44.6% | 0.2921 | ⚠️ |
| wta500 | WTA | grass | o_u_games_21.5_over | 362 | 48.1% | 0.2813 | ⚠️ |
| wta500 | WTA | hard | o_u_games_21.5_over | 2301 | 45.2% | 0.2902 | ⚠️ |

## 4. Empfehlung TENNIS_CATEGORY_MODE

| Kategorie | Aktuell | Empfehlung | Quelle |
|---|---|---|---|
| atp250 | shadow | PROMOTE → live | n=1824, gewichtete ROI=+3.7% |
| atp500 | shadow | KEEP shadow | n=637, gewichtete ROI=-2.1% |
| grand_slam | live | KEEP shadow | n=442, gewichtete ROI=-4.7% |
| m1000 | shadow | KEEP shadow | n=1138, gewichtete ROI=-3.1% |
| tour_final | shadow | KEEP shadow | n=72, gewichtete ROI=+8.5% |
| wta1000 | shadow | KEEP shadow | n=1440, gewichtete ROI=-1.7% |
| wta250 | shadow | KEEP shadow | n=1665, gewichtete ROI=+1.5% |
| wta500 | shadow | KEEP shadow | n=1010, gewichtete ROI=-2.0% |

### 4b. Surface-aware LIVE-Kombinationen (für künftige TENNIS_CATEGORY_SURFACE_MODE)

| Kategorie | Tour | Surface | N | ROI | Verdict |
|---|---|---|---:|---:|---|
| atp250 | ATP | clay | 676 | +3.8% | ✅ LIVE |
| atp250 | ATP | hard | 914 | +5.3% | ✅ LIVE |
| atp500 | ATP | grass | 112 | +18.6% | ✅ LIVE |
| wta1000 | WTA | clay | 382 | +8.4% | ✅ LIVE |
| wta250 | WTA | grass | 258 | +16.0% | ✅ LIVE |
| wta500 | WTA | grass | 147 | +8.1% | ✅ LIVE |

**Hinweis**: Aktuelle `TENNIS_CATEGORY_MODE` gruppiert nur nach Kategorie. Für surface-präzise Schaltung müsste ein neues `TENNIS_CATEGORY_SURFACE_MODE` eingeführt werden (Roadmap-Item, vermutlich J2-H).

## 5. Markt-Aktivierungs-Heuristik

- **Match Winner**: Live = Sektion-1-Verdict pro (cat, tour, surface).
- **Set-Märkte** (O/U Sets, Set Betting): bleiben SHADOW solange Brier-Kalibrierung nicht via 30+ Live-Bets bestätigt (siehe `scripts/tennis_gate_review.py`).
- **Game-Märkte** (O/U Games): wie Set-Märkte, konservativer da MC-Sim Hold-Approximation nutzt.