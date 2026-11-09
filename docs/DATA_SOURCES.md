# Data sources

All inputs are public U.S. government datasets. They are not stored in this
repository; download them into `Inputs/` before running the pipeline. The
area-water files are downloaded automatically by `analyze_rural_shares.py`.

## 1. Summary

| Dataset | Publisher | Vintage | Retrieved | Local file |
|---|---|---|---|---|
| Intermodal Freight Facilities, Rail TOFC/COFC (NTAD) | Bureau of Transportation Statistics | Published 2024-07-10; catalog item last modified 2026-07-15 | 2026-11-09 | `NTAD_Intermodal_Freight_Facilities_Rail_TOFC_COFC_*.geojson` |
| 2020 Census Urban Areas (UAC20) | U.S. Census Bureau, TIGER/Line | 2020 delineation, 2024 release | 2026-11-09 | `tl_2024_us_uac20.zip` |
| Cartographic Boundary File, States, 1:500,000 | U.S. Census Bureau | 2018 | 2026-11-09 | `cb_2018_us_state_500k.zip` |
| Area Hydrography (AREAWATER), county files | U.S. Census Bureau, TIGER/Line | 2024 release (one county on 2023, see 5) | 2026-11-09 | `Inputs/tl_2024_areawater/` |
| County and county-equivalent codes | U.S. Census Bureau | 2020 | 2026-11-09 | fetched at run time |

## 2. Rail TOFC/COFC terminals

- **Dataset page:** <https://geodata.bts.gov/datasets/e6332597fcd546d3899f60ec9b9157b9>
- **ArcGIS item:** <https://www.arcgis.com/home/item.html?id=e6332597fcd546d3899f60ec9b9157b9>
- **Feature service:** <https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_Intermodal_Freight_Facilities_Rail_TOFC_COFC/FeatureServer>
- **ArcGIS item ID:** `e6332597fcd546d3899f60ec9b9157b9`
- **Download:** GeoJSON from the dataset page (Download > GeoJSON).
- **Published:** July 10, 2024 (BTS catalog). The date is not embedded in the file.
- **Records:** 241 points, EPSG:4326. Fields used: `OBJECTID`, `TERMINAL`,
  `CITY`, `STATE`, `RAIL_CO`, `LAT`, `LON`.

### Terminal inclusion criteria

1. **Universe.** Every record in the BTS layer is considered. No filter is
   applied by railroad, equipment type, port affiliation or size.
2. **Operating status is not assumed.** The source has no status field. A
   listed record is treated as a listed facility, not as a verified active
   service point.
3. **Nearest-neighbor candidates.** A record qualifies if its geometry is
   present, its coordinates are in range, and its `STATE` is one of the
   50 states or DC. Candidates are national: terminals outside the five-state
   study region remain eligible, so border areas are measured to the truly
   nearest facility (for example Georgetown, KY; Columbus, OH; Greensboro, NC;
   Camden, NJ).
4. **Quality checks, records retained.** Duplicate locations, duplicate
   name/address records, mismatches between geometry and `LAT`/`LON`, and points
   more than 2 km outside their stated state are flagged rather than dropped.
   In this vintage all 241 records passed and all are candidates.
5. **Display set.** `terminals.geojson` contains the 40 candidates within
   100 km of the study region or nearest to at least one rural polygon.
6. **Labels.** Many `TERMINAL` values are generic ("Terminal", "Seaport"). The
   display label appends `(CITY, STATE)` when the name does not already contain
   the city. Source spellings are retained.

## 3. Urban areas

- **Download:** <https://www2.census.gov/geo/tiger/TIGER2024/UAC20/tl_2024_us_uac20.zip>
- **Vintage:** 2020 Census urban area delineation (TIGER/Line 2024 release).
- **Use:** removed from the study area to define rural territory. Rural means
  outside a 2020 Census urban area, under the Census Bureau's 2020 criteria
  (Census Bureau 2022).

## 4. State boundaries

- **Download:** <https://www2.census.gov/geo/tiger/GENZ2018/shp/cb_2018_us_state_500k.zip>
- **Vintage:** 2018 cartographic boundary file, 1:500,000, clipped to the shoreline.
- **Use:** study boundary (PA, MD, DE, VA, WV), state allocation of results, and
  terminal coordinate checks. Shoreline clipping removes the open Chesapeake Bay
  and Lake Erie but retains inland water; see section 5.

## 5. Area water

- **Download pattern:** `https://www2.census.gov/geo/tiger/TIGER2024/AREAWATER/tl_2024_{STATEFP}{COUNTYFP}_areawater.zip`
- **County list:** <https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt>
- **Coverage:** all 282 counties and independent cities in the five states.
- **Substitution:** the Census web firewall rejected the 2024 file for Tazewell
  County, VA (51185); the 2023 release was used for that county.
- **Use:** identifies water inside mapped rural territory (5,707 km², 2.0%) and
  supports the land-only sensitivity shares.

## 6. Basemap

Basemap tiles and labels: © Mapbox, © OpenStreetMap contributors.

## References

U.S. Census Bureau (2022). "Urban Area Criteria for the 2020 Census: Final
Criteria." *Federal Register* 87(57): 16706-16715.
