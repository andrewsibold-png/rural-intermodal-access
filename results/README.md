# Results

Curated outputs from the pipeline. Every statistic here was produced by the
scripts in the repository root and passed the validation checks recorded in
`qa_report.json`.

| File | Contents |
|---|---|
| `mid_atlantic_rural_intermodal_access.png` | Finished map, Mapbox Studio, with attribution |
| `rural_area_shares.csv` | Area-weighted shares of rural territory beyond 50 and 100 miles, region and by state, mapped and land-only |
| `rural_area_shares.json` | Same results with the rural-land verification, water summary and method notes |
| `rural_area_shares_water_bodies.csv` | The 25 largest named water bodies inside mapped rural territory |
| `qa_report.json` | Pipeline QA: geometry checks, terminal checks, five validation checks, within-cell variation, export precision |
| `metadata.json` | Source vintages, grid, distance method and band definitions |

## Field notes for `rural_area_shares.csv`

| Field | Meaning |
|---|---|
| `mapped_rural_km2` | Rural polygon area in EPSG:5070 |
| `missing_distance_km2` | Area without a distance (excluded from denominators; zero in this run) |
| `gt50_km2`, `gt100_km2` | Area with `distance_mi` strictly greater than 50 or 100 |
| `pct_gt50`, `pct_gt100` | Share of assessed rural area |
| `water_km2`, `pct_water` | TIGER AREAWATER inside rural territory |
| `land_only_pct_*` | Shares after removing water from numerator and denominator |

These are shares of **territory**. They are not shares of residents,
businesses or freight.
