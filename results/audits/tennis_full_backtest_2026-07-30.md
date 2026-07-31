# Tennis Full Backtest — 2026-07-30

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
| atp250 | ATP | clay | o_u_sets_2.5_over | 1973 | 38.9% | 0.2466 | ⚠️ |
| atp250 | ATP | clay | score_0-2 | 1973 | — | 0.0621 | ✅ |
| atp250 | ATP | clay | score_1-1 | 11 | — | 1.0000 | ⚠️ |
| atp250 | ATP | clay | score_1-2 | 1973 | — | 0.0570 | ✅ |
| atp250 | ATP | clay | score_2-0 | 1973 | — | 0.3475 | ⚠️ |
| atp250 | ATP | clay | score_2-1 | 1973 | — | 0.2598 | ⚠️ |
| atp250 | ATP | grass | o_u_sets_2.5_over | 605 | 35.0% | 0.2478 | ⚠️ |
| atp250 | ATP | grass | score_0-2 | 605 | — | 0.0608 | ✅ |
| atp250 | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| atp250 | ATP | grass | score_1-2 | 605 | — | 0.0584 | ✅ |
| atp250 | ATP | grass | score_2-0 | 605 | — | 0.3673 | ⚠️ |
| atp250 | ATP | grass | score_2-1 | 605 | — | 0.2412 | ✅ |
| atp250 | ATP | hard | o_u_sets_2.5_over | 3104 | 35.4% | 0.2451 | ⚠️ |
| atp250 | ATP | hard | score_0-2 | 3104 | — | 0.0611 | ✅ |
| atp250 | ATP | hard | score_1-1 | 22 | — | 1.0000 | ⚠️ |
| atp250 | ATP | hard | score_1-2 | 3104 | — | 0.0553 | ✅ |
| atp250 | ATP | hard | score_2-0 | 3104 | — | 0.3559 | ⚠️ |
| atp250 | ATP | hard | score_2-1 | 3104 | — | 0.2435 | ✅ |
| atp500 | ATP | clay | o_u_sets_2.5_over | 444 | 35.1% | 0.2421 | ✅ |
| atp500 | ATP | clay | score_0-2 | 444 | — | 0.0568 | ✅ |
| atp500 | ATP | clay | score_1-1 | 1 | — | 1.0000 | ⚠️ |
| atp500 | ATP | clay | score_1-2 | 444 | — | 0.0521 | ✅ |
| atp500 | ATP | clay | score_2-0 | 444 | — | 0.3489 | ⚠️ |
| atp500 | ATP | clay | score_2-1 | 444 | — | 0.2408 | ✅ |
| atp500 | ATP | grass | o_u_sets_2.5_over | 303 | 38.6% | 0.2459 | ⚠️ |
| atp500 | ATP | grass | score_0-2 | 303 | — | 0.0563 | ✅ |
| atp500 | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| atp500 | ATP | grass | score_1-2 | 303 | — | 0.0526 | ✅ |
| atp500 | ATP | grass | score_2-0 | 303 | — | 0.3355 | ⚠️ |
| atp500 | ATP | grass | score_2-1 | 303 | — | 0.2573 | ⚠️ |
| atp500 | ATP | hard | o_u_sets_2.5_over | 1372 | 35.3% | 0.2379 | ✅ |
| atp500 | ATP | hard | score_0-2 | 1372 | — | 0.0587 | ✅ |
| atp500 | ATP | hard | score_1-1 | 11 | — | 1.0000 | ⚠️ |
| atp500 | ATP | hard | score_1-2 | 1372 | — | 0.0478 | ✅ |
| atp500 | ATP | hard | score_2-0 | 1372 | — | 0.3310 | ⚠️ |
| atp500 | ATP | hard | score_2-1 | 1372 | — | 0.2438 | ✅ |
| grand_slam | ATP | grass | o_u_sets_2.5_over | 5 | 100.0% | 0.2924 | ⚠️ |
| grand_slam | ATP | grass | o_u_sets_3.5_over | 619 | 54.9% | 0.2783 | ⚠️ |
| grand_slam | ATP | grass | score_0-2 | 6 | — | 0.1956 | ✅ |
| grand_slam | ATP | grass | score_0-3 | 619 | — | 0.0130 | ✅ |
| grand_slam | ATP | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_1-2 | 5 | — | 0.0386 | ✅ |
| grand_slam | ATP | grass | score_1-3 | 619 | — | 0.0280 | ✅ |
| grand_slam | ATP | grass | score_2-0 | 8 | — | 0.4691 | ⚠️ |
| grand_slam | ATP | grass | score_2-1 | 7 | — | 0.3395 | ⚠️ |
| grand_slam | ATP | grass | score_2-2 | 1 | — | 1.0000 | ⚠️ |
| grand_slam | ATP | grass | score_2-3 | 619 | — | 0.0285 | ✅ |
| grand_slam | ATP | grass | score_3-0 | 623 | — | 0.3159 | ⚠️ |
| grand_slam | ATP | grass | score_3-1 | 619 | — | 0.2422 | ✅ |
| grand_slam | ATP | grass | score_3-2 | 620 | — | 0.1707 | ✅ |
| grand_slam | WTA | grass | o_u_sets_2.5_over | 621 | 31.9% | 0.2443 | ✅ |
| grand_slam | WTA | grass | score_0-2 | 621 | — | 0.0538 | ✅ |
| grand_slam | WTA | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| grand_slam | WTA | grass | score_1-2 | 621 | — | 0.0530 | ✅ |
| grand_slam | WTA | grass | score_2-0 | 621 | — | 0.3584 | ⚠️ |
| grand_slam | WTA | grass | score_2-1 | 621 | — | 0.2273 | ✅ |
| m1000 | ATP | clay | o_u_sets_2.5_over | 1083 | 36.7% | 0.2421 | ✅ |
| m1000 | ATP | clay | score_0-2 | 1083 | — | 0.0594 | ✅ |
| m1000 | ATP | clay | score_1-1 | 4 | — | 1.0000 | ⚠️ |
| m1000 | ATP | clay | score_1-2 | 1083 | — | 0.0520 | ✅ |
| m1000 | ATP | clay | score_2-0 | 1083 | — | 0.3424 | ⚠️ |
| m1000 | ATP | clay | score_2-1 | 1083 | — | 0.2492 | ⚠️ |
| m1000 | ATP | hard | o_u_sets_2.5_over | 2151 | 36.5% | 0.2407 | ✅ |
| m1000 | ATP | hard | score_0-2 | 2151 | — | 0.0615 | ✅ |
| m1000 | ATP | hard | score_1-1 | 7 | — | 1.0000 | ⚠️ |
| m1000 | ATP | hard | score_1-2 | 2151 | — | 0.0499 | ✅ |
| m1000 | ATP | hard | score_2-0 | 2151 | — | 0.3314 | ⚠️ |
| m1000 | ATP | hard | score_2-1 | 2151 | — | 0.2524 | ⚠️ |
| tour_final | ATP | hard | o_u_sets_2.5_over | 88 | 35.2% | 0.2326 | ✅ |
| tour_final | ATP | hard | score_0-2 | 88 | — | 0.0507 | ✅ |
| tour_final | ATP | hard | score_1-2 | 88 | — | 0.0446 | ✅ |
| tour_final | ATP | hard | score_2-0 | 88 | — | 0.3030 | ⚠️ |
| tour_final | ATP | hard | score_2-1 | 88 | — | 0.2483 | ⚠️ |
| tour_final | WTA | hard | o_u_sets_2.5_over | 90 | 34.4% | 0.2437 | ✅ |
| tour_final | WTA | hard | score_0-2 | 90 | — | 0.0628 | ✅ |
| tour_final | WTA | hard | score_1-2 | 90 | — | 0.0547 | ✅ |
| tour_final | WTA | hard | score_2-0 | 90 | — | 0.3686 | ⚠️ |
| tour_final | WTA | hard | score_2-1 | 90 | — | 0.2369 | ✅ |
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
| wta250 | WTA | clay | o_u_sets_2.5_over | 1412 | 34.3% | 0.2480 | ⚠️ |
| wta250 | WTA | clay | score_0-2 | 1412 | — | 0.0601 | ✅ |
| wta250 | WTA | clay | score_1-1 | 13 | — | 1.0000 | ⚠️ |
| wta250 | WTA | clay | score_1-2 | 1412 | — | 0.0588 | ✅ |
| wta250 | WTA | clay | score_2-0 | 1412 | — | 0.3731 | ⚠️ |
| wta250 | WTA | clay | score_2-1 | 1412 | — | 0.2351 | ✅ |
| wta250 | WTA | grass | o_u_sets_2.5_over | 577 | 35.9% | 0.2480 | ⚠️ |
| wta250 | WTA | grass | score_0-2 | 577 | — | 0.0590 | ✅ |
| wta250 | WTA | grass | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| wta250 | WTA | grass | score_1-2 | 577 | — | 0.0580 | ✅ |
| wta250 | WTA | grass | score_2-0 | 577 | — | 0.3611 | ⚠️ |
| wta250 | WTA | grass | score_2-1 | 577 | — | 0.2449 | ✅ |
| wta250 | WTA | hard | o_u_sets_2.5_over | 2444 | 34.7% | 0.2466 | ⚠️ |
| wta250 | WTA | hard | score_0-2 | 2444 | — | 0.0599 | ✅ |
| wta250 | WTA | hard | score_1-1 | 20 | — | 1.0000 | ⚠️ |
| wta250 | WTA | hard | score_1-2 | 2444 | — | 0.0571 | ✅ |
| wta250 | WTA | hard | score_2-0 | 2444 | — | 0.3641 | ⚠️ |
| wta250 | WTA | hard | score_2-1 | 2444 | — | 0.2390 | ✅ |
| wta500 | WTA | clay | o_u_sets_2.5_over | 482 | 33.0% | 0.2451 | ⚠️ |
| wta500 | WTA | clay | score_0-2 | 482 | — | 0.0573 | ✅ |
| wta500 | WTA | clay | score_1-1 | 3 | — | 1.0000 | ⚠️ |
| wta500 | WTA | clay | score_1-2 | 482 | — | 0.0553 | ✅ |
| wta500 | WTA | clay | score_2-0 | 482 | — | 0.3656 | ⚠️ |
| wta500 | WTA | clay | score_2-1 | 482 | — | 0.2305 | ✅ |
| wta500 | WTA | grass | o_u_sets_2.5_over | 282 | 38.7% | 0.2455 | ⚠️ |
| wta500 | WTA | grass | score_0-2 | 282 | — | 0.0618 | ✅ |
| wta500 | WTA | grass | score_1-1 | 2 | — | 1.0000 | ⚠️ |
| wta500 | WTA | grass | score_1-2 | 282 | — | 0.0573 | ✅ |
| wta500 | WTA | grass | score_2-0 | 282 | — | 0.3436 | ⚠️ |
| wta500 | WTA | grass | score_2-1 | 282 | — | 0.2600 | ⚠️ |
| wta500 | WTA | hard | o_u_sets_2.5_over | 1698 | 33.3% | 0.2442 | ✅ |
| wta500 | WTA | hard | score_0-2 | 1698 | — | 0.0579 | ✅ |
| wta500 | WTA | hard | score_1-1 | 7 | — | 1.0000 | ⚠️ |
| wta500 | WTA | hard | score_1-2 | 1698 | — | 0.0532 | ✅ |
| wta500 | WTA | hard | score_2-0 | 1698 | — | 0.3587 | ⚠️ |
| wta500 | WTA | hard | score_2-1 | 1698 | — | 0.2338 | ✅ |

## 3. Game-Märkte Kalibrierung (Brier, MC-Sim)

| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |
|---|---|---|---|---:|---:|---:|---|
| atp250 | ATP | clay | o_u_games_21.5_over | 1973 | 54.9% | 0.2641 | ⚠️ |
| atp250 | ATP | grass | o_u_games_21.5_over | 605 | 62.1% | 0.2399 | ✅ |
| atp250 | ATP | hard | o_u_games_21.5_over | 3104 | 54.7% | 0.2639 | ⚠️ |
| atp500 | ATP | clay | o_u_games_21.5_over | 444 | 50.7% | 0.2767 | ⚠️ |
| atp500 | ATP | grass | o_u_games_21.5_over | 303 | 65.3% | 0.2280 | ✅ |
| atp500 | ATP | hard | o_u_games_21.5_over | 1372 | 54.7% | 0.2573 | ⚠️ |
| grand_slam | ATP | grass | o_u_games_21.5_over | 5 | 100.0% | 0.1085 | ✅ |
| grand_slam | ATP | grass | o_u_games_38.5_over | 619 | 43.9% | 0.2801 | ⚠️ |
| grand_slam | WTA | grass | o_u_games_21.5_over | 621 | 42.2% | 0.3003 | ⚠️ |
| m1000 | ATP | clay | o_u_games_21.5_over | 1083 | 53.0% | 0.2672 | ⚠️ |
| m1000 | ATP | hard | o_u_games_21.5_over | 2151 | 55.7% | 0.2555 | ⚠️ |
| tour_final | ATP | hard | o_u_games_21.5_over | 88 | 52.3% | 0.2600 | ⚠️ |
| tour_final | WTA | hard | o_u_games_21.5_over | 90 | 46.7% | 0.2854 | ⚠️ |
| wta1000 | WTA | clay | o_u_games_21.5_over | 775 | 44.1% | 0.2925 | ⚠️ |
| wta1000 | WTA | hard | o_u_games_21.5_over | 2200 | 46.4% | 0.2831 | ⚠️ |
| wta250 | WTA | clay | o_u_games_21.5_over | 1412 | 45.2% | 0.2911 | ⚠️ |
| wta250 | WTA | grass | o_u_games_21.5_over | 577 | 47.7% | 0.2830 | ⚠️ |
| wta250 | WTA | hard | o_u_games_21.5_over | 2444 | 45.5% | 0.2899 | ⚠️ |
| wta500 | WTA | clay | o_u_games_21.5_over | 482 | 43.4% | 0.2957 | ⚠️ |
| wta500 | WTA | grass | o_u_games_21.5_over | 282 | 50.7% | 0.2727 | ⚠️ |
| wta500 | WTA | hard | o_u_games_21.5_over | 1698 | 43.9% | 0.2936 | ⚠️ |

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