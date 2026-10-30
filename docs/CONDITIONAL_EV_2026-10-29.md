# Conditional net expectancy, KXSOL15M paper (SOL-v1 tape), 2026-10-29

Snapshots 45638; closes with official-feed settlement 112 (of 113 seen); counterfactual rows 5037 (1 contract per close x side x 30-s bucket, filled via sol_v1.paper_fill at best ask + 1 tick, held to settlement; no absent-side fills).
Walk-forward split at close_ts 1793278800 (2026-10-29 06:00 PT): first half = screening (IN-SAMPLE), second half = test.
Conditions examined: 42 counterfactual + 10 taken-trade cuts = 52. Bonferroni one-sided z at 5%/42 = 3.04. SE clustered by close.

All counterfactual rows: n=5037 G=112 mean=-2.30c se=0.06c t=-36.93

## Counterfactual conditions (train = IN-SAMPLE | test = out-of-sample)

| condition | train (in-sample) | test | full (in-sample) |
|---|---|---|---|
| secs_left[0,60) | n=34 G=15 mean=-1.82c se=0.15c t=-11.88 | n=46 G=18 mean=-2.05c se=0.19c t=-10.63 | n=80 G=33 mean=-1.95c se=0.13c t=-15.15 |
| secs_left[60,180) | n=197 G=38 mean=-1.76c se=0.10c t=-16.92 | n=226 G=45 mean=-1.82c se=0.11c t=-16.56 | n=423 G=83 mean=-1.79c se=0.08c t=-23.65 |
| secs_left[180,300) | n=286 G=43 mean=-1.77c se=0.28c t=-6.41 | n=323 G=47 mean=-1.81c se=0.10c t=-18.40 | n=609 G=90 mean=-1.79c se=0.14c t=-12.88 |
| secs_left[300,480) | n=502 G=49 mean=-2.11c se=0.13c t=-16.51 | n=546 G=49 mean=-2.17c se=0.20c t=-11.01 | n=1048 G=98 mean=-2.14c se=0.12c t=-18.01 |
| secs_left[480,900) | n=1426 G=55 mean=-2.55c se=0.10c t=-26.65 | n=1451 G=56 mean=-2.54c se=0.14c t=-18.36 | n=2877 G=111 mean=-2.54c se=0.08c t=-30.31 |
| |z|[0,0.5) | n=1267 G=55 mean=-2.65c se=0.12c t=-22.69 | n=1203 G=55 mean=-2.89c se=0.12c t=-23.24 | n=2470 G=110 mean=-2.77c se=0.09c t=-32.35 |
| |z|[0.5,1) | n=561 G=48 mean=-2.44c se=0.06c t=-40.42 | n=560 G=50 mean=-2.28c se=0.15c t=-15.37 | n=1121 G=98 mean=-2.36c se=0.08c t=-29.50 |
| |z|[1,2) | n=453 G=36 mean=-1.46c se=0.10c t=-15.19 | n=559 G=44 mean=-1.52c se=0.18c t=-8.55 | n=1012 G=80 mean=-1.49c se=0.11c t=-14.04 |
| |z|[2,99) | n=164 G=30 mean=-1.36c se=0.02c t=-63.10 | n=270 G=37 mean=-1.31c se=0.02c t=-70.62 | n=434 G=67 mean=-1.33c se=0.01c t=-90.54 |
| px[0,0.1) | n=301 G=38 mean=-1.77c se=2.30c t=-0.77 | n=395 G=47 mean=-1.31c se=2.60c t=-0.50 | n=696 G=85 mean=-1.51c se=1.77c t=-0.85 |
| px[0.1,0.3) | n=397 G=54 mean=-7.34c se=4.27c t=-1.72 | n=370 G=51 mean=-7.13c se=3.93c t=-1.81 | n=767 G=105 mean=-7.24c se=2.90c t=-2.50 |
| px[0.3,0.5) | n=523 G=52 mean=-1.60c se=3.83c t=-0.42 | n=545 G=55 mean=-4.81c se=4.10c t=-1.17 | n=1068 G=107 mean=-3.24c se=2.80c t=-1.16 |
| px[0.5,0.7) | n=564 G=52 mean=-2.12c se=3.22c t=-0.66 | n=553 G=54 mean=-1.55c se=3.69c t=-0.42 | n=1117 G=106 mean=-1.84c se=2.43c t=-0.75 |
| px[0.7,0.9) | n=409 G=56 mean=-0.01c se=4.59c t=-0.00 | n=385 G=53 mean=+2.94c se=4.06c t=+0.73 | n=794 G=109 mean=+1.42c se=3.06c t=+0.46 |
| px[0.9,1) | n=251 G=38 mean=-0.49c se=3.46c t=-0.14 | n=344 G=46 mean=-1.32c se=3.44c t=-0.38 | n=595 G=84 mean=-0.97c se=2.45c t=-0.39 |
| side=yes | n=1226 G=56 mean=-10.52c se=4.82c t=-2.18 | n=1308 G=56 mean=-11.49c se=4.53c t=-2.54 | n=2534 G=112 mean=-11.02c se=3.29c t=-3.35 |
| side=no | n=1219 G=56 mean=+5.98c se=4.90c t=+1.22 | n=1284 G=56 mean=+7.07c se=4.65c t=+1.52 | n=2503 G=112 mean=+6.54c se=3.36c t=+1.95 |
| spread<=0.02 | n=2037 G=56 mean=-2.14c se=0.08c t=-25.38 | n=2266 G=56 mean=-2.18c se=0.11c t=-20.54 | n=4303 G=112 mean=-2.16c se=0.07c t=-31.58 |
| spread>0.02 | n=408 G=50 mean=-3.09c se=0.03c t=-108.88 | n=326 G=47 mean=-3.10c se=0.02c t=-126.55 | n=734 G=97 mean=-3.09c se=0.02c t=-162.31 |
| depth>=100 | n=1777 G=56 mean=-3.26c se=0.44c t=-7.38 | n=1890 G=56 mean=-2.93c se=0.54c t=-5.39 | n=3667 G=112 mean=-3.09c se=0.35c t=-8.81 |
| depth<100 | n=668 G=56 mean=+0.27c se=1.12c t=+0.24 | n=702 G=56 mean=-0.59c se=1.39c t=-0.42 | n=1370 G=112 mean=-0.17c se=0.90c t=-0.19 |
| sigma<=median | n=2052 G=50 mean=-2.31c se=0.09c t=-26.75 | n=468 G=13 mean=-2.36c se=0.23c t=-10.23 | n=2520 G=63 mean=-2.32c se=0.08c t=-28.47 |
| sigma>median | n=393 G=11 mean=-2.20c se=0.14c t=-15.91 | n=2124 G=51 mean=-2.28c se=0.11c t=-20.83 | n=2517 G=62 mean=-2.27c se=0.09c t=-23.98 |
| partial_started | n=32 G=13 mean=-1.85c se=0.16c t=-11.52 | n=41 G=17 mean=-2.04c se=0.19c t=-10.70 | n=73 G=30 mean=-1.95c se=0.13c t=-15.31 |
| partial_not_started | n=2413 G=56 mean=-2.30c se=0.08c t=-29.83 | n=2551 G=56 mean=-2.30c se=0.10c t=-23.01 | n=4964 G=112 mean=-2.30c se=0.06c t=-36.33 |
| hourPT[0,6) | n=631 G=16 mean=-2.38c se=0.11c t=-20.81 | — | n=631 G=16 mean=-2.38c se=0.11c t=-20.81 |
| hourPT[6,12) | — | n=1089 G=24 mean=-2.30c se=0.15c t=-15.03 | n=1089 G=24 mean=-2.30c se=0.15c t=-15.03 |
| hourPT[12,18) | n=711 G=16 mean=-2.49c se=0.17c t=-14.97 | n=1112 G=24 mean=-2.21c se=0.15c t=-14.37 | n=1823 G=40 mean=-2.32c se=0.12c t=-20.16 |
| hourPT[18,24) | n=1103 G=24 mean=-2.12c se=0.11c t=-19.86 | n=391 G=8 mean=-2.53c se=0.23c t=-11.20 | n=1494 G=32 mean=-2.23c se=0.10c t=-21.91 |
| model_edge>=0.0 | n=667 G=56 mean=-2.53c se=3.23c t=-0.78 | n=576 G=56 mean=-2.32c se=3.54c t=-0.65 | n=1243 G=112 mean=-2.43c se=2.38c t=-1.02 |
| model_edge>=0.02 | n=379 G=53 mean=-1.63c se=4.10c t=-0.40 | n=237 G=50 mean=-3.94c se=4.18c t=-0.94 | n=616 G=103 mean=-2.52c se=2.99c t=-0.84 |
| model_edge>=0.05 | n=98 G=31 mean=-1.64c se=6.74c t=-0.24 | n=49 G=22 mean=-7.08c se=5.91c t=-1.20 | n=147 G=53 mean=-3.46c se=4.91c t=-0.70 |
| model_edge>=0.1 | n=9 G=6 mean=+26.54c se=18.90c t=+1.40 | n=5 G=3 mean=-10.62c se=2.70c t=-3.93 | n=14 G=9 mean=+13.27c se=15.80c t=+0.84 |
| model_edge<0 | n=1778 G=56 mean=-2.21c se=1.22c t=-1.81 | n=2016 G=56 mean=-2.29c se=1.04c t=-2.21 | n=3794 G=112 mean=-2.25c se=0.79c t=-2.85 |
| model_edge>=0 & px[0,0.1) | n=121 G=30 mean=-3.41c se=1.86c t=-1.83 | n=123 G=34 mean=-4.09c se=2.40c t=-1.71 | n=244 G=64 mean=-3.76c se=1.51c t=-2.49 |
| model_edge>=0 & px[0.1,0.3) | n=287 G=52 mean=-6.12c se=4.62c t=-1.32 | n=220 G=44 mean=-6.29c se=4.91c t=-1.28 | n=507 G=96 mean=-6.19c se=3.36c t=-1.84 |
| model_edge>=0 & px[0.3,0.7) | n=239 G=48 mean=+2.90c se=4.34c t=+0.67 | n=186 G=50 mean=+1.06c se=6.02c t=+0.18 | n=425 G=98 mean=+2.09c se=3.57c t=+0.59 |
| model_edge>=0 & px[0.7,1) | n=20 G=8 mean=-10.53c se=18.88c t=-0.56 | n=47 G=13 mean=+7.54c se=0.89c t=+8.47 | n=67 G=21 mean=+2.15c se=5.96c t=+0.36 |
| model_edge>=0 & secs_left[0,60) | n=7 G=6 mean=+4.37c se=11.17c t=+0.39 | n=10 G=8 mean=+7.30c se=9.54c t=+0.77 | n=17 G=14 mean=+6.09c se=6.91c t=+0.88 |
| model_edge>=0 & secs_left[60,180) | n=56 G=26 mean=-4.32c se=6.66c t=-0.65 | n=41 G=23 mean=+2.89c se=6.39c t=+0.45 | n=97 G=49 mean=-1.28c se=4.62c t=-0.28 |
| model_edge>=0 & secs_left[180,480) | n=230 G=48 mean=-6.99c se=3.84c t=-1.82 | n=229 G=43 mean=-4.35c se=3.35c t=-1.30 | n=459 G=91 mean=-5.67c se=2.53c t=-2.24 |
| model_edge>=0 & secs_left[480,900) | n=374 G=55 mean=+0.35c se=4.83c t=+0.07 | n=296 G=53 mean=-1.79c se=5.18c t=-0.35 | n=670 G=108 mean=-0.60c se=3.51c t=-0.17 |

Screened in-sample (t>1.645, n>=30): none
Walk-forward survivors (test t > 3.04, n>=30): none
Full-sample Bonferroni-positive (IN-SAMPLE, for reference only): none

## Taken trades (SOL-v1 deterministic paper; per-contract, close-clustered)

| cut | positions | pnl $ | per contract |
|---|---|---|---|
| all | 71 | -10.79 | n=71 G=60 mean=-1.69c se=2.98c t=-0.57 |
| end=exit | 35 | +64.51 | n=35 G=30 mean=+22.08c se=2.38c t=+9.26 |
| end=settle | 36 | -75.30 | n=36 G=36 mean=-24.79c se=1.67c t=-14.88 |
| px<0.10 | 4 | -2.63 | n=4 G=4 mean=-8.06c se=1.38c t=-5.84 |
| px[0.10,0.30) | 36 | -5.61 | n=36 G=33 mean=-1.70c se=4.35c t=-0.39 |
| px>=0.30 | 31 | -2.55 | n=31 G=27 mean=-0.84c se=4.88c t=-0.17 |
| side=yes | 34 | -3.05 | n=34 G=34 mean=-0.87c se=4.31c t=-0.20 |
| side=no | 37 | -7.74 | n=37 G=37 mean=-2.44c se=4.55c t=-0.54 |
| secs_left>=300 | 41 | -6.35 | n=41 G=38 mean=-1.90c se=3.64c t=-0.52 |
| secs_left<300 | 30 | -4.44 | n=30 G=30 mean=-1.39c se=5.07c t=-0.27 |

All taken-trade cuts have <30 positions except possibly 'all'; nothing here is an 'optimal' rule. See JSON for full numbers.