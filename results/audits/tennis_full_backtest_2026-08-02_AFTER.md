# Tennis Full Backtest — 2026-08-02

Generiert von `scripts/tennis_full_backtest.py`. Datenbasis: tennis-data.co.uk Full-Tour-XLSX (Match-Outcomes + B365/Avg/Max-Odds + per-Set-Game-Scores). Elo wird walk-forward aus denselben XLSX-Daten aufgebaut (Sackmann-Repos sind ab 2026-06 nicht mehr öffentlich verfügbar).

## 1. Match Winner (ROI-validiert)

Live-Gate: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30. BLACKLIST: ROI≤-5%.

| Kategorie | Tour | Surface | N | Hit% | ROI | Brier | Verdict |
|---|---|---|---:|---:|---:|---:|---|
| atp250 | ATP | clay | 620 | 38.7% | -3.9% | 0.2514 | ⚠️ SHADOW |
| atp250 | ATP | grass | 228 | 36.4% | -4.0% | 0.2535 | ⚠️ SHADOW |
| atp250 | ATP | hard | 728 | 40.1% | -6.4% | 0.2488 | 🚫 BLACKLIST |
| atp500 | ATP | clay | 144 | 42.4% | +0.1% | 0.2396 | ⚠️ SHADOW |
| atp500 | ATP | grass | 114 | 43.0% | +12.9% | 0.2538 | ✅ LIVE |
| atp500 | ATP | hard | 384 | 42.4% | -9.2% | 0.2415 | 🚫 BLACKLIST |
| grand_slam | ATP | grass | 207 | 40.1% | +0.1% | 0.2435 | ⚠️ SHADOW |
| grand_slam | WTA | grass | 239 | 39.7% | -3.8% | 0.2451 | ⚠️ SHADOW |
| m1000 | ATP | clay | 419 | 39.9% | -8.1% | 0.2462 | 🚫 BLACKLIST |
| m1000 | ATP | hard | 655 | 47.0% | -0.2% | 0.2456 | ⚠️ SHADOW |
| tour_final | ATP | hard | 24 | 58.3% | +42.2% | 0.2560 | ⚠️ SHADOW |
| tour_final | WTA | hard | 34 | 35.3% | -11.3% | 0.2719 | 🚫 BLACKLIST |
| wta1000 | WTA | clay | 394 | 43.9% | +3.0% | 0.2447 | ⚠️ SHADOW |
| wta1000 | WTA | hard | 1027 | 42.2% | -6.8% | 0.2451 | 🚫 BLACKLIST |
| wta250 | WTA | clay | 401 | 39.4% | -6.7% | 0.2388 | 🚫 BLACKLIST |
| wta250 | WTA | grass | 261 | 41.8% | +7.6% | 0.2519 | ✅ LIVE |
| wta250 | WTA | hard | 791 | 38.7% | -6.7% | 0.2460 | 🚫 BLACKLIST |
| wta500 | WTA | clay | 214 | 41.1% | -0.3% | 0.2426 | ⚠️ SHADOW |
| wta500 | WTA | grass | 144 | 40.3% | +3.0% | 0.2440 | ⚠️ SHADOW |
| wta500 | WTA | hard | 552 | 42.8% | -3.7% | 0.2441 | ⚠️ SHADOW |

## 2. Set-Märkte Kalibrierung (Brier, keine ROI — keine historischen Quoten)

Kalibriert: Brier < 0.245

| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |
|---|---|---|---|---:|---:|---:|---|
| atp250 | ATP | clay | o_u_sets_2.5_over | 5084 | 38.7% | 0.2472 | ⚠️ |
| atp250 | ATP | clay | score_0-2 | 5084 | — | 0.0619 | ✅ |
| atp250 | ATP | clay | score_1-1 | 22 | — | 1.0000 | ⚠️ |
| atp250 | ATP | clay | score_1-2 | 5084 | — | 0.0570 | ✅ |
| atp250 | ATP | clay | score_2-0 | 5084 | — | 0.3503 | ⚠️ |
| atp250 | ATP | clay | score_2-1 | 5084 | — | 0.2585 | ⚠️ |
| atp250 | ATP | grass | o_u_sets_2.5_over | 1597 | 35.5% | 0.2477 | ⚠️ |
| atp250 | ATP | grass | score_0-2 | 1597 | — | 0.0606 | ✅ |
| atp250 | ATP | grass | score_1-1 | 5 | — | 1.0000 | ⚠️ |
| atp250 | ATP | grass | score_1-2 | 1597 | — | 0.0578 | ✅ |
| atp250 | ATP | grass | score_2-0 | 1597 | — | 0.3667 | ⚠️ |
| atp250 | ATP | grass | score_2-1 | 1597 | — | 0.2427 | ✅ |
| atp250 | ATP | hard | o_u_sets_2.5_over | 6979 | 36.2% | 0.2449 | ✅ |
| atp250 | ATP | hard | score_0-2 | 6979 | — | 0.0609 | ✅ |
| atp250 | ATP | hard | score_1-1 | 49 | — | 1.0000 | ⚠️ |
| atp250 | ATP | hard | score_1-2 | 6979 | — | 0.0539 | ✅ |
| atp250 | ATP | hard | score_2-0 | 6979 | — | 0.3506 | ⚠️ |
| atp250 | ATP | hard | score_2-1 | 6979 | — | 0.2474 | ⚠️ |
| atp500 | ATP | clay | o_u_sets_2.5_over | 1329 | 35.2% | 0.2410 | ✅ |
| atp500 | ATP | clay | score_0-2 | 1329 | — | 0.0602 | ✅ |
| atp500 | ATP | clay | score_1-1 | 7 | — | 1.0000 | ⚠️ |
| atp500 | ATP | clay | score_1-2 | 1329 | — | 0.0526 | ✅ |
| atp500 | ATP | clay | score_2-0 | 1329 | — | 0.3480 | ⚠️ |
| atp500 | ATP | clay | score_2-1 | 1329 | — | 0.2421 | ✅ |
| atp500 | ATP | grass | o_u_sets_2.5_over | 753 | 39.7% | 0.2441 | ✅ |
| atp500 | ATP | grass | score_0-2 | 753 | — | 0.0578 | ✅ |
| atp500 | ATP | grass | score_1-1 | 6 | — | 1.0000 | ⚠️ |
| atp500 | ATP | grass | score_1-2 | 753 | — | 0.0521 | ✅ |
| atp500 | ATP | grass | score_2-0 | 753 | — | 0.3256 | ⚠️ |
| atp500 | ATP | grass | score_2-1 | 753 | — | 0.2647 | ⚠️ |
| atp500 | ATP | hard | o_u_sets_2.5_over | 3304 | 34.4% | 0.2384 | ✅ |
| atp500 | ATP | hard | score_0-2 | 3304 | — | 0.0580 | ✅ |
| atp500 | ATP | hard | score_1-1 | 24 | — | 1.0000 | ⚠️ |
| atp500 | ATP | hard | score_1-2 | 3304 | — | 0.0471 | ✅ |
| atp500 | ATP | hard | score_2-0 | 3304 | — | 0.3321 | ⚠️ |
| atp500 | ATP | hard | score_2-1 | 3304 | — | 0.2403 | ✅ |
| grand_slam | ATP | grass | o_u_sets_2.5_over | 13 | 100.0% | 0.2808 | ⚠️ |
| grand_slam | ATP | grass | o_u_sets_3.5_over | 1607 | 51.6% | 0.2893 | ⚠️ |
| grand_slam | ATP | grass | score_0-2 | 14 | — | 0.1137 | ✅ |
| grand_slam | ATP | grass | score_0-3 | 1607 | — | 0.0129 | ✅ |
| grand_slam | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_1-2 | 14 | — | 0.1153 | ✅ |
| grand_slam | ATP | grass | score_1-3 | 1607 | — | 0.0273 | ✅ |
| grand_slam | ATP | grass | score_2-0 | 24 | — | 0.5250 | ⚠️ |
| grand_slam | ATP | grass | score_2-1 | 18 | — | 0.3281 | ⚠️ |
| grand_slam | ATP | grass | score_2-2 | 6 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_2-3 | 1607 | — | 0.0276 | ✅ |
| grand_slam | ATP | grass | score_3-0 | 1616 | — | 0.3302 | ⚠️ |
| grand_slam | ATP | grass | score_3-1 | 1607 | — | 0.2317 | ✅ |
| grand_slam | ATP | grass | score_3-2 | 1611 | — | 0.1617 | ✅ |
| grand_slam | WTA | grass | o_u_sets_2.5_over | 1626 | 32.7% | 0.2438 | ✅ |
| grand_slam | WTA | grass | score_0-2 | 1626 | — | 0.0538 | ✅ |
| grand_slam | WTA | grass | score_1-1 | 7 | — | 1.0000 | ⚠️ |
| grand_slam | WTA | grass | score_1-2 | 1626 | — | 0.0526 | ✅ |
| grand_slam | WTA | grass | score_2-0 | 1626 | — | 0.3541 | ⚠️ |
| grand_slam | WTA | grass | score_2-1 | 1626 | — | 0.2309 | ✅ |
| m1000 | ATP | clay | o_u_sets_2.5_over | 2438 | 36.3% | 0.2414 | ✅ |
| m1000 | ATP | clay | score_0-2 | 2438 | — | 0.0577 | ✅ |
| m1000 | ATP | clay | score_1-1 | 10 | — | 1.0000 | ⚠️ |
| m1000 | ATP | clay | score_1-2 | 2438 | — | 0.0502 | ✅ |
| m1000 | ATP | clay | score_2-0 | 2438 | — | 0.3375 | ⚠️ |
| m1000 | ATP | clay | score_2-1 | 2438 | — | 0.2480 | ⚠️ |
| m1000 | ATP | hard | o_u_sets_2.5_over | 5061 | 36.4% | 0.2396 | ✅ |
| m1000 | ATP | hard | score_0-2 | 5061 | — | 0.0609 | ✅ |
| m1000 | ATP | hard | score_1-1 | 23 | — | 1.0000 | ⚠️ |
| m1000 | ATP | hard | score_1-2 | 5061 | — | 0.0488 | ✅ |
| m1000 | ATP | hard | score_2-0 | 5061 | — | 0.3281 | ⚠️ |
| m1000 | ATP | hard | score_2-1 | 5061 | — | 0.2516 | ⚠️ |
| tour_final | ATP | hard | o_u_sets_2.5_over | 192 | 34.9% | 0.2346 | ✅ |
| tour_final | ATP | hard | score_0-2 | 192 | — | 0.0558 | ✅ |
| tour_final | ATP | hard | score_1-2 | 192 | — | 0.0451 | ✅ |
| tour_final | ATP | hard | score_2-0 | 192 | — | 0.3250 | ⚠️ |
| tour_final | ATP | hard | score_2-1 | 192 | — | 0.2423 | ✅ |
| tour_final | WTA | hard | o_u_sets_2.5_over | 264 | 34.1% | 0.2450 | ✅ |
| tour_final | WTA | hard | score_0-2 | 264 | — | 0.0592 | ✅ |
| tour_final | WTA | hard | score_1-1 | 1 | — | 1.0000 | ⚠️ |
| tour_final | WTA | hard | score_1-2 | 264 | — | 0.0547 | ✅ |
| tour_final | WTA | hard | score_2-0 | 264 | — | 0.3659 | ⚠️ |
| tour_final | WTA | hard | score_2-1 | 264 | — | 0.2350 | ✅ |
| wta1000 | WTA | clay | o_u_sets_2.5_over | 962 | 35.3% | 0.2432 | ✅ |
| wta1000 | WTA | clay | score_0-2 | 962 | — | 0.0556 | ✅ |
| wta1000 | WTA | clay | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| wta1000 | WTA | clay | score_1-2 | 962 | — | 0.0528 | ✅ |
| wta1000 | WTA | clay | score_2-0 | 962 | — | 0.3472 | ⚠️ |
| wta1000 | WTA | clay | score_2-1 | 962 | — | 0.2411 | ✅ |
| wta1000 | WTA | hard | o_u_sets_2.5_over | 2487 | 35.9% | 0.2414 | ✅ |
| wta1000 | WTA | hard | score_0-2 | 2487 | — | 0.0602 | ✅ |
| wta1000 | WTA | hard | score_1-1 | 17 | — | 1.0000 | ⚠️ |
| wta1000 | WTA | hard | score_1-2 | 2487 | — | 0.0508 | ✅ |
| wta1000 | WTA | hard | score_2-0 | 2487 | — | 0.3357 | ⚠️ |
| wta1000 | WTA | hard | score_2-1 | 2487 | — | 0.2488 | ⚠️ |
| wta250 | WTA | clay | o_u_sets_2.5_over | 3882 | 34.0% | 0.2473 | ⚠️ |
| wta250 | WTA | clay | score_0-2 | 3882 | — | 0.0599 | ✅ |
| wta250 | WTA | clay | score_1-1 | 31 | — | 1.0000 | ⚠️ |
| wta250 | WTA | clay | score_1-2 | 3882 | — | 0.0580 | ✅ |
| wta250 | WTA | clay | score_2-0 | 3882 | — | 0.3725 | ⚠️ |
| wta250 | WTA | clay | score_2-1 | 3882 | — | 0.2341 | ✅ |
| wta250 | WTA | grass | o_u_sets_2.5_over | 1278 | 34.8% | 0.2481 | ⚠️ |
| wta250 | WTA | grass | score_0-2 | 1278 | — | 0.0591 | ✅ |
| wta250 | WTA | grass | score_1-1 | 7 | — | 1.0000 | ⚠️ |
| wta250 | WTA | grass | score_1-2 | 1278 | — | 0.0579 | ✅ |
| wta250 | WTA | grass | score_2-0 | 1278 | — | 0.3681 | ⚠️ |
| wta250 | WTA | grass | score_2-1 | 1278 | — | 0.2390 | ✅ |
| wta250 | WTA | hard | o_u_sets_2.5_over | 6581 | 33.7% | 0.2454 | ⚠️ |
| wta250 | WTA | hard | score_0-2 | 6581 | — | 0.0602 | ✅ |
| wta250 | WTA | hard | score_1-1 | 43 | — | 1.0000 | ⚠️ |
| wta250 | WTA | hard | score_1-2 | 6581 | — | 0.0565 | ✅ |
| wta250 | WTA | hard | score_2-0 | 6581 | — | 0.3672 | ⚠️ |
| wta250 | WTA | hard | score_2-1 | 6581 | — | 0.2341 | ✅ |
| wta500 | WTA | clay | o_u_sets_2.5_over | 1980 | 34.6% | 0.2449 | ✅ |
| wta500 | WTA | clay | score_0-2 | 1980 | — | 0.0577 | ✅ |
| wta500 | WTA | clay | score_1-1 | 18 | — | 1.0000 | ⚠️ |
| wta500 | WTA | clay | score_1-2 | 1980 | — | 0.0547 | ✅ |
| wta500 | WTA | clay | score_2-0 | 1980 | — | 0.3555 | ⚠️ |
| wta500 | WTA | clay | score_2-1 | 1980 | — | 0.2389 | ✅ |
| wta500 | WTA | grass | o_u_sets_2.5_over | 791 | 36.0% | 0.2463 | ⚠️ |
| wta500 | WTA | grass | score_0-2 | 791 | — | 0.0592 | ✅ |
| wta500 | WTA | grass | score_1-1 | 4 | — | 1.0000 | ⚠️ |
| wta500 | WTA | grass | score_1-2 | 791 | — | 0.0561 | ✅ |
| wta500 | WTA | grass | score_2-0 | 791 | — | 0.3514 | ⚠️ |
| wta500 | WTA | grass | score_2-1 | 791 | — | 0.2481 | ⚠️ |
| wta500 | WTA | hard | o_u_sets_2.5_over | 6244 | 34.3% | 0.2431 | ✅ |
| wta500 | WTA | hard | score_0-2 | 6244 | — | 0.0590 | ✅ |
| wta500 | WTA | hard | score_1-1 | 33 | — | 1.0000 | ⚠️ |
| wta500 | WTA | hard | score_1-2 | 6244 | — | 0.0523 | ✅ |
| wta500 | WTA | hard | score_2-0 | 6244 | — | 0.3517 | ⚠️ |
| wta500 | WTA | hard | score_2-1 | 6244 | — | 0.2388 | ✅ |

## 3. Game-Märkte Kalibrierung (Brier, MC-Sim)

| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |
|---|---|---|---|---:|---:|---:|---|
| atp250 | ATP | clay | o_u_games_21.5_over | 5084 | 54.0% | 0.2679 | ⚠️ |
| atp250 | ATP | grass | o_u_games_21.5_over | 1597 | 59.2% | 0.2505 | ⚠️ |
| atp250 | ATP | hard | o_u_games_21.5_over | 6979 | 55.1% | 0.2620 | ⚠️ |
| atp500 | ATP | clay | o_u_games_21.5_over | 1329 | 50.0% | 0.2776 | ⚠️ |
| atp500 | ATP | grass | o_u_games_21.5_over | 753 | 62.8% | 0.2349 | ✅ |
| atp500 | ATP | hard | o_u_games_21.5_over | 3304 | 53.2% | 0.2624 | ⚠️ |
| grand_slam | ATP | grass | o_u_games_21.5_over | 13 | 100.0% | 0.1069 | ✅ |
| grand_slam | ATP | grass | o_u_games_38.5_over | 1607 | 42.4% | 0.2832 | ⚠️ |
| grand_slam | WTA | grass | o_u_games_21.5_over | 1626 | 43.1% | 0.2968 | ⚠️ |
| m1000 | ATP | clay | o_u_games_21.5_over | 2438 | 52.2% | 0.2688 | ⚠️ |
| m1000 | ATP | hard | o_u_games_21.5_over | 5061 | 55.5% | 0.2560 | ⚠️ |
| tour_final | ATP | hard | o_u_games_21.5_over | 192 | 51.0% | 0.2682 | ⚠️ |
| tour_final | WTA | hard | o_u_games_21.5_over | 264 | 45.1% | 0.2904 | ⚠️ |
| wta1000 | WTA | clay | o_u_games_21.5_over | 962 | 43.9% | 0.2925 | ⚠️ |
| wta1000 | WTA | hard | o_u_games_21.5_over | 2487 | 46.5% | 0.2827 | ⚠️ |
| wta250 | WTA | clay | o_u_games_21.5_over | 3882 | 43.8% | 0.2952 | ⚠️ |
| wta250 | WTA | grass | o_u_games_21.5_over | 1278 | 46.7% | 0.2861 | ⚠️ |
| wta250 | WTA | hard | o_u_games_21.5_over | 6581 | 43.8% | 0.2945 | ⚠️ |
| wta500 | WTA | clay | o_u_games_21.5_over | 1980 | 44.2% | 0.2927 | ⚠️ |
| wta500 | WTA | grass | o_u_games_21.5_over | 791 | 49.3% | 0.2780 | ⚠️ |
| wta500 | WTA | hard | o_u_games_21.5_over | 6244 | 44.9% | 0.2903 | ⚠️ |

## 4. Empfehlung TENNIS_CATEGORY_MODE

| Kategorie | Aktuell | Empfehlung | Quelle |
|---|---|---|---|
| atp250 | live | BLACKLIST | n=1576, gewichtete ROI=-5.0% |
| atp500 | live | KEEP shadow | n=642, gewichtete ROI=-3.2% |
| grand_slam | live | KEEP shadow | n=446, gewichtete ROI=-2.0% |
| m1000 | live | KEEP shadow | n=1074, gewichtete ROI=-3.3% |
| tour_final | live | KEEP shadow | n=58, gewichtete ROI=+10.8% |
| wta1000 | live | KEEP shadow | n=1421, gewichtete ROI=-4.1% |
| wta250 | live | KEEP shadow | n=1453, gewichtete ROI=-4.2% |
| wta500 | live | KEEP shadow | n=910, gewichtete ROI=-1.8% |

### 4b. Surface-aware LIVE-Kombinationen (für künftige TENNIS_CATEGORY_SURFACE_MODE)

| Kategorie | Tour | Surface | N | ROI | Verdict |
|---|---|---|---:|---:|---|
| atp500 | ATP | grass | 114 | +12.9% | ✅ LIVE |
| wta250 | WTA | grass | 261 | +7.6% | ✅ LIVE |

**Hinweis**: Aktuelle `TENNIS_CATEGORY_MODE` gruppiert nur nach Kategorie. Für surface-präzise Schaltung müsste ein neues `TENNIS_CATEGORY_SURFACE_MODE` eingeführt werden (Roadmap-Item, vermutlich J2-H).

## 5. Markt-Aktivierungs-Heuristik

- **Match Winner**: Live = Sektion-1-Verdict pro (cat, tour, surface).
- **Set-Märkte** (O/U Sets, Set Betting): bleiben SHADOW solange Brier-Kalibrierung nicht via 30+ Live-Bets bestätigt (siehe `scripts/tennis_gate_review.py`).
- **Game-Märkte** (O/U Games): wie Set-Märkte, konservativer da MC-Sim Hold-Approximation nutzt.