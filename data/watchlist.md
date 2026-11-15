# Illiquid Watchlist (omni-market-watcher)

Created 2026-11-15 (UTC dating), re-issued with permanent id slugs on the 2026-11-14 run.
Max 5 items per category. At most 2 swaps per run, each logged in the change log below.
**Ids are permanent: never renamed, never reused.** They are the `item_id` in `data/illiquid.csv`,
`data/comps.csv` and `data/timeseries.csv`.

## Sports cards
| id | item |
| :--- | :--- |
| `panini-prizm-2018-luka-doncic-280-psa10` | 2018-19 Panini Prizm Luka Dončić #280 (base), PSA 10 |
| `topps-chrome-2003-lebron-james-111-psa10` | 2003-04 Topps Chrome LeBron James #111, PSA 10 |

## TCG
| id | item |
| :--- | :--- |
| `pokemon-evolving-skies-booster-box-sealed` | Pokémon Evolving Skies (SWSH07) English booster box, factory sealed |
| `pokemon-sv151-jp-booster-box-sealed` | Pokémon Scarlet & Violet 151 (sv2a) Japanese booster box, factory sealed |
| `pokemon-umbreon-vmax-215-203-psa10` | Umbreon VMAX #215/203 alt art (Evolving Skies), PSA 10 |

## LEGO / comics / retro games
| id | item |
| :--- | :--- |
| `lego-75192-ucs-millennium-falcon-sealed` | LEGO 75192 UCS Millennium Falcon, new / factory sealed |
| `lego-10179-ucs-millennium-falcon-sealed` | LEGO 10179 UCS Millennium Falcon (2007), new / factory sealed |
| `lego-10255-assembly-square-sealed` | LEGO 10255 Assembly Square (modular), new / factory sealed |
| `super-mario-64-n64-cib` | Super Mario 64 (Nintendo 64), complete in box |

## Watches
| id | item |
| :--- | :--- |
| `rolex-submariner-126610ln-full-set` | Rolex Submariner Date 126610LN, full set |
| `rolex-daytona-116500ln-full-set` | Rolex Cosmograph Daytona 116500LN (white dial), full set |
| `rolex-gmt-master-ii-126710blro-full-set` | Rolex GMT-Master II 126710BLRO "Pepsi", full set |

## GPUs
| id | item |
| :--- | :--- |
| `nvidia-rtx-4090-used` | NVIDIA GeForce RTX 4090, used, working |
| `nvidia-rtx-5090-new` | NVIDIA GeForce RTX 5090, new / sealed |

Rental-rate references (tracked in `data/timeseries.csv` as `gpu-rental-*`, not sold-comp items):
H100 SXM, H100 PCIe, B200, RTX 5090, RTX 4090 hourly rates from the Vast.ai offers API and the RunPod
pricing page.

## Change log
- **2026-11-15 (UTC dated, first run):** watchlist created.
- **2026-11-14 run:** no items added or removed. Permanent id slugs assigned to every item (the first run
  listed items by name only) and the list restated as a table. Ids now match `data/illiquid.csv`.
  Watches and sports cards stay on the list even though every price source for them is currently blocked —
  they are in the spec's asset universe, and they are reported as `LOW_LIQUIDITY_UNPRICED` rather than dropped.
