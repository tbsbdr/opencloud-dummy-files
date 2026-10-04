# Site Screening Methodology


## Purpose

A consistent, GIS-based method to find and rank new sites for wind, solar and storage. Results are exported as GeoJSON so that they can be opened in any GIS tool and previewed in the browser.


## Steps

1. **Hard constraints** – remove areas with designations, settlements + buffer (wind 1,000 m, solar 100 m, BESS 250 m), flood zone 3, airports.
2. **Resource** – wind speed at 120 m (wind), GHI (solar), grid node headroom (storage).
3. **Grid** – distance to substations with indicative headroom; voltage level.
4. **Soft constraints** – peat, landscape character, cumulative impact, protected species records.
5. **Scoring** – weighted 1–5 per criterion; sites ≥ 3.4 go into the pipeline as Screening.


## Weights

| Criterion | Wind | Solar | BESS |
|---|---|---|---|
| Resource | 25 % | 20 % | – |
| Grid | 20 % | 25 % | 45 % |
| Planning & ecology | 30 % | 25 % | 20 % |
| Land | 15 % | 20 % | 20 % |
| Access | 10 % | 10 % | 15 % |


## Change log

- 2026-03: peat added as soft constraint after Brenna Moor no-go.
- 2025-11: BESS weights added for the Fenwick screening.
