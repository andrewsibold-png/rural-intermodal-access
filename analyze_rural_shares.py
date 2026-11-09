"""Area-weighted shares of mapped rural territory beyond 50 and 100 miles
(straight-line, geodesic) from the nearest rail TOFC/COFC terminal.

Reads the existing Outputs/rural_access.geojson produced by
prepare_rural_access.py. Areas are computed in EPSG:5070 (equal area).

Interpretation
  Each polygon carries the distance measured at one interior sample point,
  so results are approximate shares of mapped rural AREA. They are not
  shares of residents, businesses, or freight.

Steps
  1. Load and validate the rural layer (fields, numeric distances, missing).
  2. Verify it represents rural land: overlap with 2020 Census urban areas,
     and area outside the study states.
  3. Identify included water using Census TIGER/Line 2024 AREAWATER
     (downloaded once per county into Inputs/tl_2024_areawater/).
  4. Intersect with state boundaries; compute shares for the region and by state,
     for mapped territory and (as a sensitivity) for land only.

Run:  python analyze_rural_shares.py
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "Inputs"
OUTPUTS = ROOT / "Outputs"
RURAL_SRC = OUTPUTS / "rural_access.geojson"
STATES_SRC = INPUTS / "cb_2018_us_state_500k.zip"
URBAN_SRC = INPUTS / "tl_2024_us_uac20.zip"
WATER_DIR = INPUTS / "tl_2024_areawater"

STUDY_STATES = ["PA", "MD", "DE", "VA", "WV"]
AREA_CRS = "EPSG:5070"
THRESHOLDS_MI = [50, 100]    # strictly greater than
COUNTY_LIST_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"
WATER_URL = "https://www2.census.gov/geo/tiger/TIGER2024/AREAWATER/tl_2024_{geoid}_areawater.zip"
URBAN_OVERLAP_TOL_PCT = 0.01  # GeoJSON coordinates are rounded to 1e-6 deg

MTFCC_WATER = {
    "H2025": "Swamp/marsh", "H2030": "Lake/pond", "H2040": "Reservoir",
    "H2041": "Treatment pond", "H2051": "Bay/estuary/gulf/sound", "H2053": "Ocean/sea",
    "H2060": "Ice mass", "H2081": "Glacier", "H3010": "Stream/river",
    "H3013": "Braided stream", "H3020": "Canal/ditch/aqueduct",
}

result: dict = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def section(title: str) -> None:
    log("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def km2(s) -> float:
    return float(np.asarray(s).sum()) / 1e6


# --------------------------------------------------------------------------
# 1. Load
# --------------------------------------------------------------------------
def load_rural() -> gpd.GeoDataFrame:
    section("1. Rural-access layer")
    if not RURAL_SRC.exists():
        sys.exit(f"Missing {RURAL_SRC}; run prepare_rural_access.py first.")
    rural = gpd.read_file(RURAL_SRC)
    missing_fields = [f for f in ["cell_id", "distance_mi", "distance_band"] if f not in rural.columns]
    if missing_fields:
        sys.exit(f"rural_access.geojson lacks fields {missing_fields}")
    log(f"  {RURAL_SRC.name}: {len(rural):,} polygons, CRS {rural.crs}")

    raw = rural["distance_mi"]
    num = pd.to_numeric(raw, errors="coerce")
    text_values = int((num.isna() & raw.notna()).sum())
    rural["distance_mi"] = num
    nonpoly = ~rural.geom_type.isin(["Polygon", "MultiPolygon"])
    invalid = ~rural.geometry.is_valid
    log(f"  Geometry: {int(nonpoly.sum())} non-polygonal, {int(invalid.sum())} invalid "
        f"(non-polygonal features carry no area and are excluded)")
    result["geometry_check"] = {"non_polygonal": int(nonpoly.sum()), "invalid": int(invalid.sum()),
                                "non_polygonal_ids": rural.loc[nonpoly, "cell_id"].tolist()}
    rural = rural[~nonpoly].copy()
    if invalid.any():
        rural["geometry"] = rural.geometry.make_valid()
    rural = rural.to_crs(AREA_CRS)
    rural["area_m2"] = rural.geometry.area
    missing = rural["distance_mi"].isna()
    result["input"] = {
        "file": RURAL_SRC.name, "polygons": int(len(rural)), "source_crs": str(gpd.read_file(RURAL_SRC, rows=1).crs),
        "distance_mi_dtype_on_read": str(raw.dtype), "non_numeric_values": text_values,
        "missing_distance_polygons": int(missing.sum()),
        "missing_distance_km2": round(km2(rural.loc[missing, "area_m2"]), 3),
        "total_area_km2": round(km2(rural["area_m2"]), 1),
    }
    log(f"  distance_mi dtype {raw.dtype}; non-numeric values {text_values}; "
        f"missing {int(missing.sum())} polygons ({result['input']['missing_distance_km2']} km2)")
    log(f"  Total mapped rural area (EPSG:5070): {result['input']['total_area_km2']:,.1f} km2")
    return rural


# --------------------------------------------------------------------------
# 2. Verify rural
# --------------------------------------------------------------------------
def verify_rural(rural: gpd.GeoDataFrame, states: gpd.GeoDataFrame) -> None:
    section("2. Verify the layer represents rural land")
    bbox = tuple(rural.to_crs("EPSG:4269").total_bounds)
    urban = gpd.read_file(f"zip://{URBAN_SRC}", bbox=bbox).to_crs(AREA_CRS)
    ov = gpd.overlay(rural[["cell_id", "geometry"]], urban[["NAME20", "geometry"]],
                     how="intersection", keep_geom_type=True)
    overlap_km2 = km2(ov.geometry.area)
    pct = 100 * overlap_km2 / result["input"]["total_area_km2"]
    ok = pct < URBAN_OVERLAP_TOL_PCT
    log(f"  Overlap with 2020 Census urban areas: {overlap_km2:.4f} km2 ({pct:.5f}% of rural area) "
        f"-> {'PASS' if ok else 'FAIL'} (tolerance {URBAN_OVERLAP_TOL_PCT}% for coordinate rounding)")
    if len(ov):
        top = ov.assign(a=ov.geometry.area / 1e6).groupby("NAME20")["a"].sum().nlargest(5)
        for name, a in top.items():
            log(f"    {name}: {a:.4f} km2")

    study_union = states.union_all()
    outside = rural.geometry.difference(study_union)
    outside_km2 = km2(outside.area)
    log(f"  Area outside the five study states: {outside_km2:.4f} km2")
    result["rural_verification"] = {
        "urban_overlap_km2": round(overlap_km2, 4), "urban_overlap_pct": round(pct, 5),
        "status": "PASS" if ok else "FAIL", "area_outside_study_states_km2": round(outside_km2, 4),
        "note": "Residual overlap reflects 6-decimal coordinate rounding in the GeoJSON export.",
    }


# --------------------------------------------------------------------------
# 3. Water
# --------------------------------------------------------------------------
def study_counties() -> pd.DataFrame:
    txt = urllib.request.urlopen(COUNTY_LIST_URL, timeout=60).read().decode("latin-1")
    df = pd.read_csv(io.StringIO(txt), sep="|", dtype=str)
    df = df[df["STATE"].isin(STUDY_STATES)].copy()
    df["GEOID"] = df["STATEFP"] + df["COUNTYFP"]
    return df


def load_water() -> gpd.GeoDataFrame:
    section("3. Water areas (TIGER/Line 2024 AREAWATER)")
    WATER_DIR.mkdir(exist_ok=True)
    counties = study_counties()
    log(f"  {len(counties)} counties/county-equivalents in study states")
    fetched, failed = 0, []
    for geoid in counties["GEOID"]:
        path = WATER_DIR / f"tl_2024_{geoid}_areawater.zip"
        if path.exists() and zipfile.is_zipfile(path):
            continue
        path.unlink(missing_ok=True)
        error = None
        for attempt in range(4):
            try:
                data = urllib.request.urlopen(WATER_URL.format(geoid=geoid), timeout=120).read()
                if not zipfile.is_zipfile(io.BytesIO(data)):
                    # Census firewall can answer with an HTML "Request Rejected" page.
                    raise ValueError(f"response is not a zip ({data[:60]!r})")
                path.write_bytes(data)
                fetched += 1
                error = None
                break
            except Exception as exc:  # retried, then reported
                error = exc
                time.sleep(5 * (attempt + 1))
        if error is not None:
            # Fall back to the prior TIGER vintage; record the substitution.
            try:
                url23 = WATER_URL.replace("2024", "2023").format(geoid=geoid)
                data = urllib.request.urlopen(url23, timeout=120).read()
                if not zipfile.is_zipfile(io.BytesIO(data)):
                    raise ValueError("2023 response is not a zip")
                path.write_bytes(data)
                (WATER_DIR / f"{geoid}.VINTAGE_2023").write_text(f"2024 failed: {error}\n")
                fetched += 1
            except Exception as exc2:
                failed.append(f"{geoid}: 2024 {error}; 2023 {exc2}")
    log(f"  Downloaded {fetched} new files; {len(failed)} failed")
    for f in failed:
        log(f"    FAILED {f}")

    parts = []
    for geoid in counties["GEOID"]:
        path = WATER_DIR / f"tl_2024_{geoid}_areawater.zip"
        if path.exists() and zipfile.is_zipfile(path):
            g = gpd.read_file(f"zip://{path}", columns=["HYDROID", "FULLNAME", "MTFCC"])
            g["COUNTY_GEOID"] = geoid
            parts.append(g)
    water = pd.concat(parts, ignore_index=True)
    water = gpd.GeoDataFrame(water, geometry="geometry", crs=parts[0].crs).to_crs(AREA_CRS)
    water["geometry"] = water.geometry.make_valid()
    log(f"  {len(water):,} water polygons loaded")
    result["water_source"] = {
        "dataset": "U.S. Census Bureau TIGER/Line 2024 AREAWATER (county files)",
        "counties": int(len(counties)), "files_failed": failed,
        "counties_using_2023_vintage": sorted(p.stem for p in WATER_DIR.glob("*.VINTAGE_2023")),
        "cache_dir": str(WATER_DIR.relative_to(ROOT)),
    }
    return water


# --------------------------------------------------------------------------
# 4. Shares
# --------------------------------------------------------------------------
def shares(df: pd.DataFrame, water_col: str = "water_m2") -> dict:
    """df rows: polygon pieces with area_m2, water_m2, distance_mi."""
    have = df["distance_mi"].notna()
    out = {
        "mapped_rural_km2": km2(df["area_m2"]),
        "missing_distance_km2": km2(df.loc[~have, "area_m2"]),
        "assessed_km2": km2(df.loc[have, "area_m2"]),
        "water_km2": km2(df[water_col]),
    }
    a = df.loc[have]
    for t in THRESHOLDS_MI:
        sel = a["distance_mi"] > t
        out[f"gt{t}_km2"] = km2(a.loc[sel, "area_m2"])
        out[f"pct_gt{t}"] = 100 * out[f"gt{t}_km2"] / out["assessed_km2"] if out["assessed_km2"] else np.nan
        land_den = km2(a["area_m2"] - a[water_col])
        land_num = km2(a.loc[sel, "area_m2"] - a.loc[sel, water_col])
        out[f"land_only_pct_gt{t}"] = 100 * land_num / land_den if land_den else np.nan
    out["pct_water"] = 100 * out["water_km2"] / out["mapped_rural_km2"] if out["mapped_rural_km2"] else np.nan
    return out


def main() -> None:
    states = gpd.read_file(f"zip://{STATES_SRC}")
    states = states[states["STUSPS"].isin(STUDY_STATES)].to_crs(AREA_CRS)[["STUSPS", "NAME", "geometry"]]

    rural = load_rural()
    verify_rural(rural, states)
    water = load_water()

    section("4. Intersect with states and water")
    by_state = gpd.overlay(rural[["cell_id", "distance_mi", "geometry"]], states, how="intersection",
                           keep_geom_type=True)
    by_state["area_m2"] = by_state.geometry.area
    split_cells = int(by_state["cell_id"].duplicated(keep=False).sum())
    unassigned_km2 = result["input"]["total_area_km2"] - km2(by_state["area_m2"])
    log(f"  {len(by_state):,} state pieces ({split_cells:,} from polygons crossing a state line); "
        f"area not assigned to a state: {unassigned_km2:.4f} km2")

    by_state["piece"] = np.arange(len(by_state))
    wov = gpd.overlay(by_state[["piece", "STUSPS", "distance_mi", "geometry"]],
                      water[["HYDROID", "FULLNAME", "MTFCC", "geometry"]],
                      how="intersection", keep_geom_type=True)
    wov["water_m2"] = wov.geometry.area
    by_state["water_m2"] = by_state["piece"].map(wov.groupby("piece")["water_m2"].sum()).fillna(0.0)
    # Guard against overlapping water polygons double counting.
    by_state["water_m2"] = np.minimum(by_state["water_m2"], by_state["area_m2"])
    log(f"  Water inside mapped rural territory: {km2(by_state['water_m2']):,.1f} km2")

    rows = [{"scope": "Study region (PA, MD, DE, VA, WV)", **shares(by_state)}]
    for st in STUDY_STATES:
        rows.append({"scope": st, **shares(by_state[by_state["STUSPS"] == st])})
    table = pd.DataFrame(rows)

    # Water detail.
    wov["type"] = wov["MTFCC"].map(MTFCC_WATER).fillna(wov["MTFCC"])
    wov["FULLNAME"] = wov["FULLNAME"].fillna("(unnamed)")
    by_type = (wov.groupby(["STUSPS", "type"])["water_m2"].sum() / 1e6).round(2).rename("km2").reset_index()
    named = (wov[wov["FULLNAME"] != "(unnamed)"].groupby(["FULLNAME", "STUSPS", "type"])["water_m2"]
             .sum().div(1e6).round(2).rename("km2").reset_index().nlargest(25, "km2"))

    section("5. Results: approximate shares of mapped rural area")
    fmt = table.copy()
    show = fmt[["scope", "assessed_km2", "missing_distance_km2", "pct_gt50", "pct_gt100",
                "pct_water", "land_only_pct_gt50", "land_only_pct_gt100"]]
    with pd.option_context("display.width", 200, "display.float_format", "{:,.2f}".format):
        log(show.to_string(index=False))
    log("\n  Water by type within rural territory (km2):")
    with pd.option_context("display.width", 200):
        log(by_type.pivot(index="type", columns="STUSPS", values="km2").fillna(0)
            .reindex(columns=STUDY_STATES, fill_value=0).to_string())
    log("\n  Largest named water bodies inside rural territory:")
    log(named.head(10).to_string(index=False))

    OUTPUTS.mkdir(exist_ok=True)
    table.round(3).to_csv(OUTPUTS / "rural_area_shares.csv", index=False)
    named.to_csv(OUTPUTS / "rural_area_shares_water_bodies.csv", index=False)
    result["results"] = table.round(3).to_dict(orient="records")
    result["water_by_type_km2"] = by_type.to_dict(orient="records")
    result["method"] = {
        "area_crs": AREA_CRS,
        "measure": "Area-weighted share of mapped rural territory with distance_mi strictly greater "
                   "than 50 and 100 miles (straight-line geodesic distance to nearest rail TOFC/COFC terminal).",
        "denominator": "Mapped rural area with a non-missing distance; missing-distance area reported separately.",
        "state_assignment": "Rural polygons intersected with 2018 Census cartographic state boundaries; "
                            "polygons crossing a state line are split and their area allocated by location.",
        "sample_point_caveat": "Each polygon's distance is measured at one interior sample point and applied to "
                               "the whole polygon; within a 5-km cell the true distance varies by about 2 mi. "
                               "Shares are therefore approximate.",
        "water": "Mapped rural territory follows shoreline-clipped state boundaries, so it includes inland "
                 "water (lakes, reservoirs, rivers). Land-only shares remove TIGER AREAWATER polygons "
                 "and are a sensitivity check.",
        "scope_limit": "Results describe territory (area) only. They are not shares of residents, "
                       "businesses, establishments or freight volume.",
    }
    (OUTPUTS / "rural_area_shares.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    log("\n  Wrote rural_area_shares.csv, rural_area_shares_water_bodies.csv, rural_area_shares.json")


if __name__ == "__main__":
    main()
