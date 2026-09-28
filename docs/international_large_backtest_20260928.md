# Large causal international football backtest

Generated: `2026-09-28T21:19:15.646443+00:00`
Evaluated against main: `13c02a1c84c03733835e849e06e64f7c19e9c622`

## Dataset

- Source: `https://raw.githubusercontent.com/martj42/international_results/master/results.csv` (martj42/international_results)
- Fetched: `2026-09-28T17:53:57.885534+00:00`
- Raw rows: 49,547; SHA-256: `df35268f8fc341ff7fb93d448b4e40356676ac35300a6b4461fd199a99ac1514`
- Analyzed date range: 1872-11-30 through 2026-08-26
- Competitive: 19,498; friendlies: 18,383; conflicting fixture rows excluded: 2
- UEFA team set: 58 canonical teams (source-derived + SportsBrain confederation map)
- Modern international universe: 10,091 fixtures from 2016; 6,169 from 2020.
- One identical fixture key had contradictory source scores; both rows were excluded from training and scoring.

## Causal model results

Full-history counts are descriptive only. Proper-score interpretation emphasizes 2016+ and 2020+.

Eligible fixtures and model-specific evaluated count/coverage are shown separately; DC excludes fixtures with teams absent from that block's fitted model.

| Cohort | Window | Eligible | DC eval / cov. | DC Brier | DC log | DC acc. | Elo eval / cov. | Elo Brier | Elo log | Elo acc. | Empirical Brier |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| uefa_nations_league | 2000_onward | 658 | 652 / 99.1% | 0.6389 | 1.0860 | 0.4985 | 658 / 100.0% | 0.5973 | 1.0234 | 0.5532 | 0.6601 |
| uefa_nations_league | 2010_onward | 658 | 652 / 99.1% | 0.6389 | 1.0860 | 0.4985 | 658 / 100.0% | 0.5973 | 1.0234 | 0.5532 | 0.6601 |
| uefa_nations_league | 2016_onward | 658 | 652 / 99.1% | 0.6389 | 1.0860 | 0.4985 | 658 / 100.0% | 0.5973 | 1.0234 | 0.5532 | 0.6601 |
| uefa_nations_league | 2020_onward | 516 | 516 / 100.0% | 0.6201 | 1.0519 | 0.5155 | 516 / 100.0% | 0.6109 | 1.0400 | 0.5349 | 0.6677 |
| uefa_nations_league | 2025 complete year | 28 | 28 / 100.0% | 0.6596 | 1.0877 | 0.5714 | 28 / 100.0% | 0.6418 | 1.0888 | 0.4643 | 0.6383 |
| uefa_competitive_non_nl | 2000_onward | 3,978 | 3,945 / 99.2% | 0.5306 | 0.9183 | 0.5959 | 3,978 / 100.0% | 0.4780 | 0.8500 | 0.6533 | 0.6474 |
| uefa_competitive_non_nl | 2010_onward | 2,466 | 2,444 / 99.1% | 0.5210 | 0.9001 | 0.6027 | 2,466 / 100.0% | 0.4723 | 0.8399 | 0.6582 | 0.6459 |
| uefa_competitive_non_nl | 2016_onward | 1,561 | 1,543 / 98.8% | 0.5143 | 0.8905 | 0.6118 | 1,561 / 100.0% | 0.4565 | 0.8172 | 0.6726 | 0.6412 |
| uefa_competitive_non_nl | 2020_onward | 931 | 931 / 100.0% | 0.5040 | 0.8666 | 0.6187 | 931 / 100.0% | 0.4751 | 0.8490 | 0.6617 | 0.6478 |
| uefa_competitive_non_nl | 2025 complete year | 192 | 192 / 100.0% | 0.4090 | 0.7126 | 0.7188 | 192 / 100.0% | 0.3871 | 0.7056 | 0.7188 | 0.6297 |
| all_competitive | 2000_onward | 12,264 | 12,074 / 98.5% | 0.5852 | 1.0069 | 0.5474 | 12,264 / 100.0% | 0.5100 | 0.8981 | 0.6227 | 0.6307 |
| all_competitive | 2010_onward | 8,156 | 8,079 / 99.1% | 0.5855 | 1.0064 | 0.5478 | 8,156 / 100.0% | 0.5162 | 0.9081 | 0.6177 | 0.6364 |
| all_competitive | 2016_onward | 5,711 | 5,640 / 98.8% | 0.5866 | 1.0095 | 0.5489 | 5,711 / 100.0% | 0.5129 | 0.9032 | 0.6188 | 0.6375 |
| all_competitive | 2020_onward | 3,751 | 3,751 / 100.0% | 0.5646 | 0.9718 | 0.5700 | 3,751 / 100.0% | 0.5221 | 0.9183 | 0.6113 | 0.6427 |
| all_competitive | 2025 complete year | 619 | 619 / 100.0% | 0.4733 | 0.8059 | 0.6688 | 619 / 100.0% | 0.4649 | 0.8283 | 0.6446 | 0.6299 |
| friendlies | 2000_onward | 8,445 | 8,094 / 95.8% | 0.6689 | 1.1458 | 0.4616 | 8,445 / 100.0% | 0.5996 | 1.0338 | 0.5363 | 0.6408 |
| friendlies | 2010_onward | 5,070 | 4,891 / 96.5% | 0.6606 | 1.1308 | 0.4694 | 5,070 / 100.0% | 0.5838 | 1.0102 | 0.5542 | 0.6350 |
| friendlies | 2016_onward | 2,859 | 2,784 / 97.4% | 0.6538 | 1.1309 | 0.4802 | 2,859 / 100.0% | 0.5794 | 1.0067 | 0.5649 | 0.6326 |
| friendlies | 2020_onward | 1,592 | 1,580 / 99.2% | 0.6163 | 1.0620 | 0.5101 | 1,592 / 100.0% | 0.5509 | 0.9639 | 0.5923 | 0.6229 |
| friendlies | 2025 complete year | 221 | 220 / 99.5% | 0.5989 | 1.0142 | 0.5000 | 221 / 100.0% | 0.5576 | 0.9874 | 0.5792 | 0.6210 |
| combined_all_international_diagnostic | 2000_onward | 25,485 | 23,808 / 93.4% | 0.6247 | 1.0725 | 0.5069 | 25,485 / 100.0% | 0.5444 | 0.9487 | 0.5886 | 0.6346 |
| combined_all_international_diagnostic | 2010_onward | 15,956 | 15,034 / 94.2% | 0.6193 | 1.0623 | 0.5130 | 15,956 / 100.0% | 0.5413 | 0.9449 | 0.5925 | 0.6356 |
| combined_all_international_diagnostic | 2016_onward | 10,091 | 9,610 / 95.2% | 0.6136 | 1.0580 | 0.5220 | 10,091 / 100.0% | 0.5353 | 0.9372 | 0.5996 | 0.6357 |
| combined_all_international_diagnostic | 2020_onward | 6,169 | 6,060 / 98.2% | 0.5842 | 1.0061 | 0.5483 | 6,169 / 100.0% | 0.5297 | 0.9297 | 0.6059 | 0.6358 |
| combined_all_international_diagnostic | 2025 complete year | 1,002 | 976 / 97.4% | 0.5180 | 0.8781 | 0.6076 | 1,002 / 100.0% | 0.5029 | 0.8912 | 0.6188 | 0.6295 |

### Non-overlapping modern eras

| Cohort | Era | Eligible | DC eval / cov. | DC Brier | Elo Brier | Empirical Brier |
|---|---|---:|---:|---:|---:|---:|
| uefa_nations_league | 2000_2009 | 0 | 0 / 0.0% | — | — | — |
| uefa_nations_league | 2010_2015 | 0 | 0 / 0.0% | — | — | — |
| uefa_nations_league | 2016_2019 | 142 | 136 / 95.8% | 0.7101 | 0.5482 | 0.6326 |
| uefa_nations_league | 2020_2025 | 516 | 516 / 100.0% | 0.6201 | 0.6109 | 0.6677 |
| uefa_competitive_non_nl | 2000_2009 | 1,512 | 1,501 / 99.3% | 0.5461 | 0.4873 | 0.6498 |
| uefa_competitive_non_nl | 2010_2015 | 905 | 901 / 99.6% | 0.5325 | 0.4995 | 0.6540 |
| uefa_competitive_non_nl | 2016_2019 | 630 | 612 / 97.1% | 0.5300 | 0.4290 | 0.6314 |
| uefa_competitive_non_nl | 2020_2025 | 851 | 851 / 100.0% | 0.4854 | 0.4706 | 0.6473 |
| all_competitive | 2000_2009 | 4,108 | 3,995 / 97.2% | 0.5846 | 0.4979 | 0.6194 |
| all_competitive | 2010_2015 | 2,445 | 2,439 / 99.8% | 0.5830 | 0.5238 | 0.6336 |
| all_competitive | 2016_2019 | 1,960 | 1,889 / 96.4% | 0.6304 | 0.4953 | 0.6277 |
| all_competitive | 2020_2025 | 3,615 | 3,615 / 100.0% | 0.5610 | 0.5226 | 0.6433 |
| friendlies | 2000_2009 | 3,375 | 3,203 / 94.9% | 0.6816 | 0.6233 | 0.6496 |
| friendlies | 2010_2015 | 2,211 | 2,107 / 95.3% | 0.6696 | 0.5894 | 0.6380 |
| friendlies | 2016_2019 | 1,267 | 1,204 / 95.0% | 0.7029 | 0.6153 | 0.6448 |
| friendlies | 2020_2025 | 1,384 | 1,372 / 99.1% | 0.6143 | 0.5531 | 0.6210 |

## Tournament-class results (2016 onward)

Class-specific Brier, log loss, accuracy and ECE. N and coverage are model-specific.

| Tournament class | Model | Eval / eligible | Coverage | Brier | Log loss | Accuracy | ECE |
|---|---|---:|---:|---:|---:|---:|---:|
| uefa_nations_league | dixon_coles | 652 / 658 | 99.1% | 0.6389 | 1.0860 | 0.4985 | 0.0776 |
| uefa_nations_league | elo | 658 / 658 | 100.0% | 0.5973 | 1.0234 | 0.5532 | 0.0821 |
| uefa_nations_league | empirical_frequency | 658 / 658 | 100.0% | 0.6601 | 1.0874 | 0.4301 | 0.0545 |
| world_cup | dixon_coles | 232 / 232 | 100.0% | 0.6955 | 1.2091 | 0.4483 | 0.1437 |
| world_cup | elo | 232 / 232 | 100.0% | 0.5646 | 0.9814 | 0.5862 | 0.0828 |
| world_cup | empirical_frequency | 232 / 232 | 100.0% | 0.6528 | 1.0756 | 0.4397 | 0.0459 |
| world_cup_qualification | dixon_coles | 2,328 / 2,338 | 99.6% | 0.5513 | 0.9518 | 0.5842 | 0.0674 |
| world_cup_qualification | elo | 2,338 / 2,338 | 100.0% | 0.4811 | 0.8565 | 0.6463 | 0.0738 |
| world_cup_qualification | empirical_frequency | 2,338 / 2,338 | 100.0% | 0.6325 | 1.0483 | 0.4803 | 0.0202 |
| uefa_euro | dixon_coles | 153 / 153 | 100.0% | 0.6824 | 1.1364 | 0.4379 | 0.1102 |
| uefa_euro | elo | 153 / 153 | 100.0% | 0.6397 | 1.0986 | 0.5163 | 0.1225 |
| uefa_euro | empirical_frequency | 153 / 153 | 100.0% | 0.6855 | 1.1233 | 0.3856 | 0.0848 |
| uefa_euro_qualification | dixon_coles | 493 / 501 | 98.4% | 0.4735 | 0.8263 | 0.6511 | 0.0694 |
| uefa_euro_qualification | elo | 501 / 501 | 100.0% | 0.4110 | 0.7430 | 0.7006 | 0.0590 |
| uefa_euro_qualification | empirical_frequency | 501 / 501 | 100.0% | 0.6260 | 1.0356 | 0.4830 | 0.0506 |
| friendly | dixon_coles | 2,784 / 2,859 | 97.4% | 0.6538 | 1.1309 | 0.4802 | 0.1073 |
| friendly | elo | 2,859 / 2,859 | 100.0% | 0.5794 | 1.0067 | 0.5649 | 0.0878 |
| friendly | empirical_frequency | 2,859 / 2,859 | 100.0% | 0.6326 | 1.0519 | 0.4911 | 0.0306 |
| other_competitive | dixon_coles | 1,782 / 1,829 | 97.4% | 0.6226 | 1.0707 | 0.5157 | 0.0951 |
| other_competitive | elo | 1,829 / 1,829 | 100.0% | 0.5340 | 0.9371 | 0.5976 | 0.0727 |
| other_competitive | empirical_frequency | 1,829 / 1,829 | 100.0% | 0.6331 | 1.0495 | 0.4811 | 0.0210 |
| other_international | dixon_coles | 1,186 / 1,521 | 78.0% | 0.6478 | 1.1174 | 0.4916 | 0.1050 |
| other_international | elo | 1,521 / 1,521 | 100.0% | 0.5365 | 0.9341 | 0.5930 | 0.0583 |
| other_international | empirical_frequency | 1,521 / 1,521 | 100.0% | 0.6347 | 1.0510 | 0.4753 | 0.0243 |

### Additional senior competitive tournament detail (2016+)

Regional competitions are separated rather than hidden in the aggregate `other_competitive` class.

| Competition | Model | Eval / eligible | Brier | Log loss | Accuracy | ECE |
|---|---|---:|---:|---:|---:|---:|
| AFC Asian Cup | dixon_coles | 102 / 102 | 0.6208 | 1.0749 | 0.5196 | 0.1231 |
| AFC Asian Cup | elo | 102 / 102 | 0.5047 | 0.8881 | 0.6176 | 0.1096 |
| AFC Asian Cup | empirical_frequency | 102 / 102 | 0.6416 | 1.0596 | 0.4608 | 0.0413 |
| AFC Asian Cup qualification | dixon_coles | 190 / 190 | 0.6145 | 1.0454 | 0.5316 | 0.1255 |
| AFC Asian Cup qualification | elo | 190 / 190 | 0.4171 | 0.7494 | 0.6842 | 0.0647 |
| AFC Asian Cup qualification | empirical_frequency | 190 / 190 | 0.6028 | 1.0051 | 0.5316 | 0.0282 |
| African Cup of Nations | dixon_coles | 240 / 240 | 0.6377 | 1.0609 | 0.4958 | 0.1088 |
| African Cup of Nations | elo | 240 / 240 | 0.6401 | 1.1111 | 0.5083 | 0.1331 |
| African Cup of Nations | empirical_frequency | 240 / 240 | 0.6663 | 1.1009 | 0.4333 | 0.0647 |
| African Cup of Nations qualification | dixon_coles | 685 / 685 | 0.6302 | 1.0935 | 0.5080 | 0.1152 |
| African Cup of Nations qualification | elo | 685 / 685 | 0.5674 | 0.9958 | 0.5708 | 0.1025 |
| African Cup of Nations qualification | empirical_frequency | 685 / 685 | 0.6330 | 1.0518 | 0.4876 | 0.0251 |
| CONCACAF Nations League | dixon_coles | 396 / 422 | 0.6008 | 1.0463 | 0.5328 | 0.1070 |
| CONCACAF Nations League | elo | 422 / 422 | 0.4995 | 0.8734 | 0.6280 | 0.0521 |
| CONCACAF Nations League | empirical_frequency | 422 / 422 | 0.6251 | 1.0342 | 0.4834 | 0.0501 |
| CONCACAF Nations League qualification | dixon_coles | 47 / 68 | 0.6264 | 1.0379 | 0.4894 | 0.1543 |
| CONCACAF Nations League qualification | elo | 68 / 68 | 0.4063 | 0.7234 | 0.7059 | 0.1080 |
| CONCACAF Nations League qualification | empirical_frequency | 68 / 68 | 0.5938 | 0.9857 | 0.5294 | 0.0949 |
| Copa América | dixon_coles | 118 / 118 | 0.6380 | 1.0978 | 0.5169 | 0.1034 |
| Copa América | elo | 118 / 118 | 0.5403 | 0.9485 | 0.6017 | 0.1197 |
| Copa América | empirical_frequency | 118 / 118 | 0.6635 | 1.0943 | 0.4322 | 0.0543 |
| Copa América qualification | dixon_coles | 4 / 4 | 0.4830 | 0.8608 | 0.7500 | 0.3195 |
| Copa América qualification | elo | 4 / 4 | 0.3660 | 0.6393 | 0.7500 | 0.2274 |
| Copa América qualification | empirical_frequency | 4 / 4 | 0.4806 | 0.8302 | 0.7500 | 0.1576 |

## Neutral-site, qualifier and confidence diagnostics

The JSON contains all cohort/window strata plus home/draw/away reliability bins. Balanced means maximum class probability <0.50; moderate favorite 0.50–<0.65; strong favorite ≥0.65.

| Cohort | Dimension | Stratum | Model | N | Brier | Log loss | Accuracy | ECE |
|---|---|---|---|---:|---:|---:|---:|---:|
| uefa_nations_league | venue | neutral | dixon_coles | 34 | 0.6531 | 1.1014 | 0.5588 | 0.1855 |
| uefa_nations_league | venue | neutral | elo | 34 | 0.7496 | 1.2637 | 0.3529 | 0.2283 |
| uefa_nations_league | venue | neutral | empirical_frequency | 34 | 0.7793 | 1.2635 | 0.2353 | 0.1833 |
| uefa_nations_league | venue | non_neutral | dixon_coles | 618 | 0.6381 | 1.0851 | 0.4951 | 0.0844 |
| uefa_nations_league | venue | non_neutral | elo | 624 | 0.5890 | 1.0103 | 0.5641 | 0.0768 |
| uefa_nations_league | venue | non_neutral | empirical_frequency | 624 | 0.6537 | 1.0778 | 0.4407 | 0.0474 |
| uefa_nations_league | qualification | qualifier | dixon_coles | 0 | — | — | — | — |
| uefa_nations_league | qualification | qualifier | elo | 0 | — | — | — | — |
| uefa_nations_league | qualification | qualifier | empirical_frequency | 0 | — | — | — | — |
| uefa_nations_league | qualification | non_qualifier | dixon_coles | 652 | 0.6389 | 1.0860 | 0.4985 | 0.0776 |
| uefa_nations_league | qualification | non_qualifier | elo | 658 | 0.5973 | 1.0234 | 0.5532 | 0.0821 |
| uefa_nations_league | qualification | non_qualifier | empirical_frequency | 658 | 0.6601 | 1.0874 | 0.4301 | 0.0545 |
| uefa_competitive_non_nl | venue | neutral | dixon_coles | 311 | 0.6438 | 1.0874 | 0.4823 | 0.1021 |
| uefa_competitive_non_nl | venue | neutral | elo | 317 | 0.5640 | 0.9771 | 0.5836 | 0.0861 |
| uefa_competitive_non_nl | venue | neutral | empirical_frequency | 317 | 0.6856 | 1.1203 | 0.3754 | 0.0902 |
| uefa_competitive_non_nl | venue | non_neutral | dixon_coles | 1,232 | 0.4816 | 0.8408 | 0.6445 | 0.0550 |
| uefa_competitive_non_nl | venue | non_neutral | elo | 1,244 | 0.4291 | 0.7765 | 0.6953 | 0.0620 |
| uefa_competitive_non_nl | venue | non_neutral | empirical_frequency | 1,244 | 0.6298 | 1.0421 | 0.4783 | 0.0416 |
| uefa_competitive_non_nl | qualification | qualifier | dixon_coles | 1,223 | 0.4680 | 0.8170 | 0.6574 | 0.0514 |
| uefa_competitive_non_nl | qualification | qualifier | elo | 1,241 | 0.4176 | 0.7573 | 0.7043 | 0.0602 |
| uefa_competitive_non_nl | qualification | qualifier | empirical_frequency | 1,241 | 0.6345 | 1.0477 | 0.4674 | 0.0524 |
| uefa_competitive_non_nl | qualification | non_qualifier | dixon_coles | 320 | 0.6911 | 1.1713 | 0.4375 | 0.1221 |
| uefa_competitive_non_nl | qualification | non_qualifier | elo | 320 | 0.6076 | 1.0494 | 0.5500 | 0.1004 |
| uefa_competitive_non_nl | qualification | non_qualifier | empirical_frequency | 320 | 0.6672 | 1.0977 | 0.4188 | 0.0614 |
| all_competitive | venue | neutral | dixon_coles | 1,374 | 0.6200 | 1.0484 | 0.5160 | 0.0803 |
| all_competitive | venue | neutral | elo | 1,395 | 0.5545 | 0.9689 | 0.5835 | 0.0799 |
| all_competitive | venue | neutral | empirical_frequency | 1,395 | 0.6817 | 1.1157 | 0.3849 | 0.0840 |
| all_competitive | venue | non_neutral | dixon_coles | 4,266 | 0.5759 | 0.9970 | 0.5595 | 0.0797 |
| all_competitive | venue | non_neutral | elo | 4,316 | 0.4995 | 0.8819 | 0.6302 | 0.0674 |
| all_competitive | venue | non_neutral | empirical_frequency | 4,316 | 0.6233 | 1.0357 | 0.4986 | 0.0101 |
| all_competitive | qualification | qualifier | dixon_coles | 3,747 | 0.5596 | 0.9669 | 0.5754 | 0.0769 |
| all_competitive | qualification | qualifier | elo | 3,786 | 0.4827 | 0.8587 | 0.6429 | 0.0710 |
| all_competitive | qualification | qualifier | empirical_frequency | 3,786 | 0.6294 | 1.0437 | 0.4857 | 0.0183 |
| all_competitive | qualification | non_qualifier | dixon_coles | 1,893 | 0.6402 | 1.0938 | 0.4966 | 0.0884 |
| all_competitive | qualification | non_qualifier | elo | 1,925 | 0.5722 | 0.9906 | 0.5714 | 0.0726 |
| all_competitive | qualification | non_qualifier | empirical_frequency | 1,925 | 0.6536 | 1.0778 | 0.4416 | 0.0467 |
| friendlies | venue | neutral | dixon_coles | 822 | 0.6663 | 1.1495 | 0.4526 | 0.1072 |
| friendlies | venue | neutral | elo | 835 | 0.5935 | 1.0215 | 0.5413 | 0.0818 |
| friendlies | venue | neutral | empirical_frequency | 835 | 0.6645 | 1.0943 | 0.4251 | 0.0575 |
| friendlies | venue | non_neutral | dixon_coles | 1,962 | 0.6485 | 1.1231 | 0.4918 | 0.1094 |
| friendlies | venue | non_neutral | elo | 2,024 | 0.5736 | 1.0006 | 0.5746 | 0.0907 |
| friendlies | venue | non_neutral | empirical_frequency | 2,024 | 0.6194 | 1.0345 | 0.5183 | 0.0333 |
| friendlies | qualification | qualifier | dixon_coles | 0 | — | — | — | — |
| friendlies | qualification | qualifier | elo | 0 | — | — | — | — |
| friendlies | qualification | qualifier | empirical_frequency | 0 | — | — | — | — |
| friendlies | qualification | non_qualifier | dixon_coles | 2,784 | 0.6538 | 1.1309 | 0.4802 | 0.1073 |
| friendlies | qualification | non_qualifier | elo | 2,859 | 0.5794 | 1.0067 | 0.5649 | 0.0878 |
| friendlies | qualification | non_qualifier | empirical_frequency | 2,859 | 0.6326 | 1.0519 | 0.4911 | 0.0306 |

## Findings and launch relevance

- Classification: `MODEL_WARNING`. Research warning only; no release-size gate or production authority follows.
- The modern paired comparisons show DC worse than Elo on multiclass Brier for all competitive matches in both 2016+ and 2020+; see clustered 95% intervals below.
- UEFA competitive non-NL 2016+: DC Brier 0.5143 vs Elo 0.4565 (1,543 paired fixtures), consistent with that warning.
- NL 2016+: observed draw frequency 25.0%; DC mean draw probability 26.3%, DRAW argmax 11.2%; HOME argmax 57.1% vs observed home wins 42.9%.
- Current unsettled shadow reference is 29 candidates: 19 HOME / 0 DRAW / 10 AWAY; confidence {'LOW': 26, 'MEDIUM': 3}. It has no outcome labels and is selection-conditioned; zero DRAW candidates do not show that historical draws are absent.
- High-EV overconfidence and canonical `detect_value()` ROI/CLV are not measurable without accepted genuine pre-match odds; no market edge is inferred from model-only results.
- DC/Elo argmax disagreement (2000+): uefa_nations_league 31.1% (N=652); uefa_competitive_non_nl 24.6% (N=3,945); all_competitive 29.2% (N=12,074).
- Modern 2016+/2020+ cohort-specific DC/Elo disagreement rates are retained in JSON to distinguish competitive, friendly, NL and non-NL transfer behavior.

### Paired date-cluster bootstrap (95% intervals)

| Sample | Paired N | DC − Elo Brier | 95% interval |
|---|---:|---:|---:|
| all_competitive_2000_onward | 12,074 | 0.0732 | [0.0651, 0.0821] |
| all_competitive_2016_onward | 5,640 | 0.0725 | [0.0602, 0.0859] |
| all_competitive_2020_onward | 3,751 | 0.0425 | [0.0308, 0.0545] |
| uefa_non_nl_2016_onward | 1,543 | 0.0565 | [0.0358, 0.0767] |
| friendlies_2016_onward | 2,784 | 0.0757 | [0.0568, 0.0958] |

## UEFA team view (2016+ competitive appearances)

Counts accompany reported team metrics. Teams under 25 appearances remain count-only in JSON; 25 is a reporting convention, not a launch gate.

| Team | N appearances | DC Brier | DC accuracy | Elo Brier | Elo accuracy |
|---|---:|---:|---:|---:|---:|
| Austria | 86 | 0.5310 | 0.6163 | 0.4953 | 0.6047 |
| Belgium | 100 | 0.5240 | 0.6000 | 0.4238 | 0.7100 |
| Croatia | 101 | 0.6107 | 0.5556 | 0.5497 | 0.6040 |
| Czechia | 81 | 0.5741 | 0.5696 | 0.5068 | 0.6296 |
| Denmark | 89 | 0.6020 | 0.5169 | 0.5865 | 0.5730 |
| England | 106 | 0.5133 | 0.6058 | 0.4747 | 0.6604 |
| France | 109 | 0.6600 | 0.4312 | 0.5245 | 0.6422 |
| Germany | 85 | 0.5148 | 0.6118 | 0.4719 | 0.6353 |
| Ireland | 72 | 0.7265 | 0.4306 | 0.6100 | 0.5139 |
| Italy | 93 | 0.5925 | 0.5376 | 0.5480 | 0.5699 |
| Netherlands | 91 | 0.5450 | 0.5824 | 0.4763 | 0.6264 |
| Northern Ireland | 73 | 0.4950 | 0.6027 | 0.4842 | 0.6849 |
| Norway | 77 | 0.5359 | 0.5844 | 0.4768 | 0.6234 |
| Portugal | 102 | 0.5325 | 0.5882 | 0.5057 | 0.6176 |
| Spain | 105 | 0.5723 | 0.5524 | 0.4843 | 0.6571 |
| Switzerland | 96 | 0.6611 | 0.5000 | 0.5809 | 0.5729 |
| Turkey | 85 | 0.5903 | 0.5663 | 0.5833 | 0.6000 |
| Ukraine | 80 | 0.5389 | 0.6282 | 0.5686 | 0.6000 |

## Historical odds and signal-policy audit

- Local rolling odds cache: 14 snapshots, 0 complete 1X2 entries, 0 accepted international matches.
- Existing World Cup odds integration has no local cache. One direct public XLSX request failed at DNS and was not retried; no rows were imported.
- A public BetExplorer-derived odds candidate was rejected because it documents a team-pair/date ±1-day fuzzy join with no per-quote timestamp or exact fixture ID.
- No public row met the accepted fixture/provenance/time contract; `HISTORICAL_MARKET_SUBSET` is empty. `detect_value()`, EV, ROI, CLV and staking were not run.


## Validation limits

- Dixon-Coles: canonical model, fit on competitive results strictly before each 5-year block and frozen within that block; model-specific coverage is reported.
- Canonical DC optimizer emitted 3 parameter-bound warning(s); exact affected block/team/parameter/value/side is retained in the JSON block audit.
- Elo: point-in-time ratings; each date's fixtures are scored before any result on that date updates ratings.
- Causal GBT: unavailable because the 91-feature point-in-time market/squad/context store is absent. Frozen later-trained GBT performance is not presented as causal.
- Canonical stacker and `detect_value()`: unavailable without authentic timestamped historical 1X2 market inputs; no synthetic or current odds were used.
- See JSON for full per-class calibration, predicted/observed outcome mixes, sharpness, entropy, favorite bins, team metrics, source conflicts, and cutoff proof.

## Safety and launch relevance

- Classification: `MODEL_WARNING` — research evidence only; not an activation or publication gate.
- Provider requests: 0; credential accesses: 0; quota consumption: 0; runtime/ledger mutation: 0; activation/publication/betting/deployment: false.
