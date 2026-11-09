"""Prepare GeoJSON layers for a Mapbox Studio map of rural proximity to rail
intermodal (TOFC/COFC) terminals in PA, MD, DE, VA and WV.

Pipeline
  1. Inspect and report input schemas and coordinate systems.
  2. Dissolve the five state polygons into one study boundary.
  3. Build a 5-km square grid in EPSG:5070 (CONUS Albers, equal area).
  4. Clip the grid to the study area and subtract 2020 Census urban areas.
  5. Explode disconnected rural pieces; take an interior representative point.
  6. Find the nearest terminal (all valid U.S. terminals are candidates)
     using WGS84 geodesic distance (pyproj.Geod), processed in batches.
  7. Export rural_access.geojson, urban_areas.geojson and terminals.geojson
     in EPSG:4326, plus QA and metadata summaries.

Problem records are flagged in the QA outputs, never silently dropped.

Run:  python prepare_rural_access.py            (fresh QA sample, seed logged)
      python prepare_rural_access.py --seed N   (reproduce a prior QA sample)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import Geod

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "Inputs"
OUTPUTS = ROOT / "Outputs"

STATES_SRC = INPUTS / "cb_2018_us_state_500k.zip"
URBAN_SRC = INPUTS / "tl_2024_us_uac20.zip"
TERMINALS_SRC = INPUTS / "NTAD_Intermodal_Freight_Facilities_Rail_TOFC_COFC_5929809478813660340.geojson"

STUDY_STATES = ["PA", "MD", "DE", "VA", "WV"]
GRID_CRS = "EPSG:5070"       # NAD83 / Conus Albers (metres, equal area)
OUTPUT_CRS = "EPSG:4326"     # WGS84, required by GeoJSON / Mapbox
CELL_SIZE_M = 5_000
METERS_PER_MILE = 1609.344
BATCH_SIZE = 2_000           # representative points per geodesic batch
# Terminals within this distance of the study boundary are exported for display.
TERMINAL_DISPLAY_BUFFER_M = 100_000
COORD_PRECISION = 6          # ~0.1 m; keeps Mapbox uploads compact
SLIVER_AREA_KM2 = 0.01       # pieces below this are reported (not dropped)
STATE_MISMATCH_TOL_M = 2_000 # terminal farther than this outside its STATE polygon is flagged
ARTIFACT_AREA_M2 = 1e-6      # overlay residue; observed artifacts <= 1e-8 m2, real slivers >= 2e-4 m2

# Validation (QA) settings
QA_SEED = None               # None = fresh seed each run (logged); override with --seed N
QA_NEAR_TERMINAL_M = 5_000   # check 1: sample points this close to a terminal
QA_NEAR_TERMINAL_N = 10      # check 1: number of terminals sampled
QA_BORDER_M = 10_000         # check 2: sample points this close to the study boundary
QA_BORDER_N = 500            # check 2: border pieces re-verified by brute force
QA_TOP_DISTANT_N = 15        # check 5: most distant pieces inspected
QA_VARIATION_N = 300         # within-cell variation: full cells sampled
DIST_TOL_MI = 0.01           # rounding tolerance for distance comparisons

# Display categories only; not commercial viability thresholds.
BAND_EDGES = [25, 50, 100, 150]
BAND_LABELS = [
    "Under 25 miles",
    "25–under 50 miles",
    "50–under 100 miles",
    "100–under 150 miles",
    "150 miles or more",
]

# 50 states + DC. Territories (PR, VI, GU, ...) are reported separately.
US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

qa: dict = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def section(title: str) -> None:
    log("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


# --------------------------------------------------------------------------
# 1. Inspect inputs
# --------------------------------------------------------------------------
def inspect_inputs() -> dict:
    section("1. Input schemas and coordinate systems")
    report = {}
    for label, path in [("states", STATES_SRC), ("urban_areas", URBAN_SRC), ("terminals", TERMINALS_SRC)]:
        if not path.exists():
            sys.exit(f"Missing input: {path}")
        src = f"zip://{path}" if path.suffix == ".zip" else str(path)
        info = pyogrio.read_info(src)
        fields = dict(zip(info["fields"].tolist(), [str(d) for d in info["dtypes"]]))
        report[label] = {
            "file": path.name,
            "file_modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "crs": info["crs"],
            "geometry_type": info["geometry_type"],
            "feature_count": int(info["features"]),
            "fields": fields,
        }
        log(f"\n[{label}] {path.name}")
        log(f"  CRS: {info['crs']}   geometry: {info['geometry_type']}   features: {info['features']}")
        for name, dtype in fields.items():
            log(f"    {name:<14} {dtype}")
    return report


def require_fields(gdf: gpd.GeoDataFrame, fields: list[str], label: str) -> None:
    missing = [f for f in fields if f not in gdf.columns]
    if missing:
        sys.exit(f"{label}: expected field(s) not found: {missing}. Columns: {list(gdf.columns)}")


def validate_geometries(gdf: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
    """Report invalid/empty geometries and repair invalid ones with make_valid."""
    null_or_empty = gdf.geometry.isna() | gdf.geometry.is_empty
    invalid = ~null_or_empty & ~gdf.geometry.is_valid
    qa.setdefault("geometry_checks", {})[label] = {
        "features": int(len(gdf)),
        "null_or_empty": int(null_or_empty.sum()),
        "invalid_repaired": int(invalid.sum()),
    }
    log(f"  {label}: {len(gdf)} features, {null_or_empty.sum()} null/empty, "
        f"{invalid.sum()} invalid (repaired with make_valid)")
    if invalid.any():
        gdf = gdf.copy()
        gdf.loc[invalid, "geometry"] = shapely.make_valid(gdf.loc[invalid, "geometry"].values)
    return gdf


def polygons_only(geoms: np.ndarray) -> np.ndarray:
    """Keep only polygonal parts (intersection can yield GeometryCollections)."""
    out = []
    for g in geoms:
        if g is None or g.is_empty:
            out.append(None)
        elif g.geom_type in ("Polygon", "MultiPolygon"):
            out.append(g)
        else:
            parts = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")]
            out.append(shapely.union_all(parts) if parts else None)
    return np.array(out, dtype=object)


# --------------------------------------------------------------------------
# 2. Study boundary
# --------------------------------------------------------------------------
def load_study_area() -> gpd.GeoDataFrame:
    section("2. Study boundary")
    states = gpd.read_file(f"zip://{STATES_SRC}")
    require_fields(states, ["STUSPS", "NAME"], "states")
    sel = states[states["STUSPS"].isin(STUDY_STATES)]
    found = sorted(sel["STUSPS"])
    if found != sorted(STUDY_STATES):
        sys.exit(f"Study states missing from boundary file: {set(STUDY_STATES) - set(found)}")
    log(f"  Selected: {', '.join(sel['NAME'])}")
    sel = validate_geometries(sel, "states").to_crs(GRID_CRS)
    study = gpd.GeoDataFrame(geometry=[sel.union_all()], crs=GRID_CRS)
    area_km2 = study.area.iloc[0] / 1e6
    qa["study_area_km2"] = round(area_km2, 1)
    log(f"  Dissolved study area: {area_km2:,.0f} km2 in {GRID_CRS}")
    return study


# --------------------------------------------------------------------------
# 3-4. Grid, clip, urban subtraction
# --------------------------------------------------------------------------
def build_grid(study: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    section("3. 5-km grid")
    minx, miny, maxx, maxy = study.total_bounds
    # Snap origin to a multiple of the cell size so the grid is reproducible.
    x0 = np.floor(minx / CELL_SIZE_M) * CELL_SIZE_M
    y0 = np.floor(miny / CELL_SIZE_M) * CELL_SIZE_M
    xs = np.arange(x0, maxx, CELL_SIZE_M)
    ys = np.arange(y0, maxy, CELL_SIZE_M)
    cols, rows = np.meshgrid(np.arange(len(xs)), np.arange(len(ys)))
    cols, rows = cols.ravel(), rows.ravel()
    boxes = shapely.box(xs[cols], ys[rows], xs[cols] + CELL_SIZE_M, ys[rows] + CELL_SIZE_M)
    grid = gpd.GeoDataFrame({"row": rows, "col": cols}, geometry=boxes, crs=GRID_CRS)
    # Keep only cells touching the study area before the expensive clip.
    grid = grid[grid.intersects(study.geometry.iloc[0])].reset_index(drop=True)
    log(f"  {len(xs)} x {len(ys)} lattice; {len(grid):,} cells intersect the study area")
    qa["grid_cells_intersecting_study_area"] = int(len(grid))
    return grid


def load_urban(study: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    section("4. Urban areas (2020 Census)")
    bbox_4269 = tuple(study.to_crs("EPSG:4269").total_bounds)
    urban = gpd.read_file(f"zip://{URBAN_SRC}", bbox=bbox_4269)
    require_fields(urban, ["UACE20", "NAME20"], "urban_areas")
    urban = validate_geometries(urban, "urban_areas").to_crs(GRID_CRS)
    urban = urban[urban.intersects(study.geometry.iloc[0])].reset_index(drop=True)
    log(f"  {len(urban)} urban areas intersect the study area")
    return urban


def rural_pieces(grid: gpd.GeoDataFrame, study: gpd.GeoDataFrame, urban: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    log("  Clipping grid to study area ...")
    clipped = grid.copy()
    clipped["geometry"] = polygons_only(shapely.intersection(grid.geometry.values, study.geometry.iloc[0]))
    clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty]

    log("  Subtracting urban polygons ...")
    rural = gpd.overlay(clipped, urban[["geometry"]], how="difference", keep_geom_type=True)
    fully_urban = len(clipped) - len(rural)

    log("  Exploding disconnected pieces ...")
    rural = rural.explode(index_parts=True).reset_index(level=1, names=["", "part"])
    rural = rural.reset_index(drop=True)
    rural = rural[~rural.geometry.is_empty]
    # Overlay leaves zero-width slivers where adjacent urban polygons share an
    # edge; they lie inside urban land. Remove them and record the count.
    artifact = rural.geometry.area < ARTIFACT_AREA_M2
    qa["overlay_artifacts_removed"] = {
        "threshold_m2": ARTIFACT_AREA_M2,
        "count": int(artifact.sum()),
        "max_area_m2": float(rural.geometry.area[artifact].max()) if artifact.any() else 0.0,
        "grid_row_col": rural.loc[artifact, ["row", "col"]].astype(int).values.tolist(),
        "note": "Zero-width slivers along shared edges of adjacent urban polygons; not rural land.",
    }
    log(f"  Overlay artifacts (< {ARTIFACT_AREA_M2:g} m2) removed: {int(artifact.sum())}")
    rural = rural[~artifact]
    rural = validate_geometries(rural, "rural_pieces")

    rural = rural.sort_values(["row", "col", "part"]).reset_index(drop=True)
    rural["part"] = rural.groupby(["row", "col"]).cumcount()
    rural["cell_id"] = (
        "r" + rural["row"].astype(str).str.zfill(3)
        + "c" + rural["col"].astype(str).str.zfill(3)
        + "p" + rural["part"].astype(str)
    )
    if rural["cell_id"].duplicated().any():
        sys.exit("cell_id is not unique; aborting.")

    area_km2 = rural.geometry.area / 1e6
    qa["rural_pieces"] = {
        "clipped_cells": int(len(clipped)),
        "cells_fully_urban": int(fully_urban),
        "rural_pieces_after_explode": int(len(rural)),
        "cells_split_into_multiple_pieces": int((rural.groupby(["row", "col"]).size() > 1).sum()),
        f"slivers_under_{SLIVER_AREA_KM2}_km2_retained": int((area_km2 < SLIVER_AREA_KM2).sum()),
        "rural_area_km2": round(float(area_km2.sum()), 1),
    }
    for k, v in qa["rural_pieces"].items():
        log(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v:,.1f}")
    return rural[["cell_id", "geometry"]]


# --------------------------------------------------------------------------
# 5. Terminals
# --------------------------------------------------------------------------
def load_terminals() -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    section("5. Terminals (BTS NTAD Rail TOFC/COFC)")
    t = gpd.read_file(TERMINALS_SRC)
    require_fields(t, ["OBJECTID", "TERMINAL", "CITY", "STATE", "LAT", "LON"], "terminals")
    if t.crs is None or t.crs.to_epsg() != 4326:
        t = t.to_crs(OUTPUT_CRS)
    t = t.reset_index(drop=True)
    issues = []

    def flag(mask: pd.Series, reason: str) -> None:
        for _, r in t[mask].iterrows():
            issues.append({"OBJECTID": r["OBJECTID"], "TERMINAL": r["TERMINAL"], "CITY": r["CITY"],
                           "STATE": r["STATE"], "LAT": r["LAT"], "LON": r["LON"], "issue": reason})
        log(f"  {reason}: {int(mask.sum())}")

    geom_missing = t.geometry.isna() | t.geometry.is_empty
    gx = t.geometry.x.where(~geom_missing)
    gy = t.geometry.y.where(~geom_missing)
    out_of_range = ~geom_missing & ((gx.abs() > 180) | (gy.abs() > 90))
    attr_missing = t["LAT"].isna() | t["LON"].isna()
    mismatch = ~geom_missing & ~attr_missing & (
        ((gx - t["LON"]).abs() > 1e-4) | ((gy - t["LAT"]).abs() > 1e-4))
    non_us = ~t["STATE"].fillna("").str.strip().str.upper().isin(US_STATES)
    name_missing = t["TERMINAL"].isna() | (t["TERMINAL"].astype(str).str.strip() == "")

    flag(geom_missing, "missing geometry")
    flag(out_of_range, "coordinates out of range")
    flag(attr_missing, "missing LAT/LON attributes")
    flag(mismatch, "geometry differs from LAT/LON attributes")
    flag(non_us, "STATE not a U.S. state or DC")
    flag(name_missing, "missing TERMINAL name")

    # Duplicates: identical location (~1 m) or identical name + address + city + state.
    loc_key = gx.round(5).astype(str) + "," + gy.round(5).astype(str)
    dup_loc = ~geom_missing & loc_key.duplicated(keep=False)
    rec_key = t[["TERMINAL", "TERM_ADDRESS", "CITY", "STATE"]].astype(str).agg("|".join, axis=1)
    dup_rec = rec_key.duplicated(keep=False)
    flag(dup_loc, "duplicate location (retained; ties resolve to lowest OBJECTID)")
    flag(dup_rec, "duplicate name/address record (retained)")

    # Coordinate plausibility: point should lie in (or within tolerance of) the
    # polygon for its STATE attribute. Cartographic shorelines are generalized,
    # so port terminals can sit slightly offshore; the tolerance absorbs that.
    states = gpd.read_file(f"zip://{STATES_SRC}").to_crs(GRID_CRS).set_index("STUSPS")
    t_grid = t.to_crs(GRID_CRS)
    off_m = np.full(len(t), np.nan)
    for i, (st, g) in enumerate(zip(t["STATE"], t_grid.geometry)):
        if st in states.index and g is not None and not g.is_empty:
            off_m[i] = states.geometry[st].distance(g)
    t["km_outside_stated_state"] = np.round(off_m / 1000, 2)
    flag(pd.Series(off_m > STATE_MISMATCH_TOL_M, index=t.index),
         f"point more than {STATE_MISMATCH_TOL_M / 1000:.0f} km outside its STATE polygon (retained)")

    valid = ~geom_missing & ~out_of_range & ~non_us
    t["candidate"] = valid
    # Many TERMINAL values are generic ("Terminal", "Seaport"); others already
    # name the city. Append "(CITY, STATE)" only when the city is not present.
    def label(r: pd.Series) -> str:
        name = str(r["TERMINAL"]).strip() if pd.notna(r["TERMINAL"]) else "Unnamed terminal"
        city = str(r["CITY"]).strip() if pd.notna(r["CITY"]) else ""
        if city and city.lower() in name.lower():
            return name
        return f"{name} ({city or '?'}, {r['STATE'] if pd.notna(r['STATE']) else '?'})"

    t["display_name"] = t.apply(label, axis=1)
    qa["terminals"] = {
        "records": int(len(t)),
        "nearest_neighbor_candidates": int(valid.sum()),
        "excluded_as_candidates": int((~valid).sum()),
        "duplicate_location_records": int(dup_loc.sum()),
        "duplicate_name_address_records": int(dup_rec.sum()),
        "operating_status": "not available in source; all records treated as listed, status not assumed",
    }
    log(f"  Candidates: {valid.sum()} of {len(t)} (exclusions listed in qa_terminal_issues.csv)")
    issues_df = pd.DataFrame(issues, columns=["OBJECTID", "TERMINAL", "CITY", "STATE", "LAT", "LON", "issue"])
    return t, issues_df


# --------------------------------------------------------------------------
# 6. Nearest terminal (geodesic)
# --------------------------------------------------------------------------
GEOD = Geod(ellps="WGS84")


def geodesic_nearest(plon, plat, tlon, tlat, verbose: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Index of and distance (m) to the nearest target for each point, in batches."""
    plon, plat = np.asarray(plon, float), np.asarray(plat, float)
    tlon, tlat = np.asarray(tlon, float), np.asarray(tlat, float)
    n, m = len(plon), len(tlon)
    best_idx = np.full(n, -1, dtype=int)
    best_m = np.full(n, np.nan)
    for start in range(0, n, BATCH_SIZE):
        stop = min(start + BATCH_SIZE, n)
        k = stop - start
        _, _, dist = GEOD.inv(np.repeat(plon[start:stop], m), np.repeat(plat[start:stop], m),
                              np.tile(tlon, k), np.tile(tlat, k))
        dist = np.asarray(dist).reshape(k, m)
        idx = np.argmin(dist, axis=1)   # first minimum = lowest OBJECTID on ties
        best_idx[start:stop] = idx
        best_m[start:stop] = dist[np.arange(k), idx]
        if verbose:
            log(f"  processed {stop:,} / {n:,}")
    return best_idx, best_m


def geodesic_m(lon1, lat1, lon2, lat2) -> np.ndarray:
    return np.asarray(GEOD.inv(lon1, lat1, lon2, lat2)[2])


def nearest_terminals(rural: gpd.GeoDataFrame, terminals: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    section("6. Nearest terminal, WGS84 geodesic distance")
    cand = terminals[terminals["candidate"]].sort_values("OBJECTID").reset_index(drop=True)
    tlon, tlat = cand.geometry.x.to_numpy(), cand.geometry.y.to_numpy()

    # representative_point() is guaranteed to fall inside each polygon.
    pts = gpd.GeoSeries(rural.geometry.representative_point(), crs=GRID_CRS).to_crs(OUTPUT_CRS)
    plon, plat = pts.x.to_numpy(), pts.y.to_numpy()
    best_idx, best_m = geodesic_nearest(plon, plat, tlon, tlat, verbose=True)

    out = rural.copy()
    out["sample_lon"], out["sample_lat"] = plon, plat   # internal; not exported
    out["nearest_terminal"] = cand["display_name"].to_numpy()[best_idx]
    out["nearest_terminal_objectid"] = cand["OBJECTID"].to_numpy()[best_idx]
    out["distance_mi"] = np.round(best_m / METERS_PER_MILE, 2)
    out["distance_band"] = pd.cut(
        out["distance_mi"], bins=[-np.inf, *BAND_EDGES, np.inf], labels=BAND_LABELS, right=False
    ).astype(str)

    missing = int(out["distance_mi"].isna().sum())
    d = out["distance_mi"]
    qa["distances"] = {
        "missing_distances": missing,
        "min_mi": float(d.min()), "p25_mi": float(d.quantile(0.25)), "median_mi": float(d.median()),
        "mean_mi": round(float(d.mean()), 2), "p75_mi": float(d.quantile(0.75)), "max_mi": float(d.max()),
        "pieces_by_band": out["distance_band"].value_counts().reindex(BAND_LABELS, fill_value=0).astype(int).to_dict(),
        "area_km2_by_band": (out.geometry.area / 1e6).groupby(out["distance_band"]).sum()
                             .reindex(BAND_LABELS, fill_value=0).round(1).to_dict(),
        "distinct_nearest_terminals": int(out["nearest_terminal_objectid"].nunique()),
    }
    log(f"  Missing distances: {missing}")
    log(f"  Distance (mi): min {d.min():.1f}, median {d.median():.1f}, mean {d.mean():.1f}, max {d.max():.1f}")
    for band in BAND_LABELS:
        log(f"    {band:<24} {qa['distances']['pieces_by_band'][band]:>6,} pieces  "
            f"{qa['distances']['area_km2_by_band'][band]:>10,.0f} km2")
    return out


# --------------------------------------------------------------------------
# 6b. Validation checks
# --------------------------------------------------------------------------
def record_check(name: str, passed: bool, summary: str, **details) -> None:
    status = "PASS" if passed else "FAIL"
    qa.setdefault("validation_checks", {})[name] = {"status": status, "summary": summary, **details}
    log(f"  [{status}] {name}: {summary}")


def check_near_terminals(rural, cand, rng, samples: list) -> None:
    """1. Pieces whose sample point lies close to a terminal must have small distances."""
    pts = shapely.points(rural["sample_lon"], rural["sample_lat"])
    pts = gpd.GeoSeries(pts, crs=OUTPUT_CRS).to_crs(GRID_CRS).values
    tgeom = cand.to_crs(GRID_CRS).geometry.values
    t_idx, p_idx = shapely.STRtree(pts).query(tgeom, predicate="dwithin", distance=QA_NEAR_TERMINAL_M)
    near_terms = np.unique(t_idx)
    if len(near_terms) == 0:
        record_check("1_near_terminal_distances", False,
                     f"no rural sample point within {QA_NEAR_TERMINAL_M / 1000:.0f} km of any terminal")
        return
    chosen = rng.choice(near_terms, size=min(QA_NEAR_TERMINAL_N, len(near_terms)), replace=False)
    limit_mi = QA_NEAR_TERMINAL_M / METERS_PER_MILE
    failures = 0
    for ti in chosen:
        pi = rng.choice(p_idx[t_idx == ti])
        r = rural.iloc[pi]
        term = cand.iloc[ti]
        d_direct = geodesic_m(r.sample_lon, r.sample_lat, term.geometry.x, term.geometry.y) / METERS_PER_MILE
        # Nearest distance can never exceed the distance to this nearby terminal.
        ok = r.distance_mi <= d_direct + DIST_TOL_MI and r.distance_mi <= limit_mi + DIST_TOL_MI
        failures += not ok
        samples.append({"check": "1_near_terminal", "cell_id": r.cell_id, "sample_lon": r.sample_lon,
                        "sample_lat": r.sample_lat, "reference_terminal": term.display_name,
                        "reference_terminal_mi": round(float(d_direct), 2),
                        "nearest_terminal": r.nearest_terminal, "distance_mi": r.distance_mi,
                        "ok": ok})
        log(f"    {r.cell_id}: {r.distance_mi:5.2f} mi to {r.nearest_terminal}"
            f"  (reference {term.display_name}: {d_direct:.2f} mi)")
    record_check("1_near_terminal_distances", failures == 0,
                 f"{len(chosen) - failures}/{len(chosen)} sampled pieces within "
                 f"{QA_NEAR_TERMINAL_M / 1000:.0f} km of a terminal report <= {limit_mi:.2f} mi "
                 f"and <= the distance to that terminal",
                 terminals_with_nearby_rural_points=int(len(near_terms)))


def check_border_eligibility(rural, cand, study, rng, samples: list) -> None:
    """2. Near the study boundary, out-of-region terminals must remain eligible."""
    pts = gpd.GeoSeries(shapely.points(rural["sample_lon"], rural["sample_lat"]), crs=OUTPUT_CRS)
    dist_to_edge = pts.to_crs(GRID_CRS).distance(study.boundary.iloc[0]).to_numpy()
    border = np.flatnonzero(dist_to_edge <= QA_BORDER_M)
    out_region = ~cand["STATE"].isin(STUDY_STATES).to_numpy()
    nearest_out = rural["nearest_terminal_objectid"].isin(cand.loc[out_region, "OBJECTID"]).to_numpy()

    # Independent brute-force recheck of a sample of border pieces.
    sample = rng.choice(border, size=min(QA_BORDER_N, len(border)), replace=False)
    sub = rural.iloc[sample]
    tlon, tlat = cand.geometry.x.to_numpy(), cand.geometry.y.to_numpy()
    idx_all, d_all = geodesic_nearest(sub.sample_lon, sub.sample_lat, tlon, tlat)
    idx_in, d_in = geodesic_nearest(sub.sample_lon, sub.sample_lat, tlon[~out_region], tlat[~out_region])
    recheck_mismatch = int((cand["OBJECTID"].to_numpy()[idx_all] != sub.nearest_terminal_objectid.to_numpy()).sum())
    overstated = (d_in - d_all) / METERS_PER_MILE
    n_out_closer = int((overstated > DIST_TOL_MI).sum())

    for j in np.argsort(-overstated)[:5]:
        r = sub.iloc[j]
        samples.append({"check": "2_border", "cell_id": r.cell_id, "sample_lon": r.sample_lon,
                        "sample_lat": r.sample_lat, "nearest_terminal": r.nearest_terminal,
                        "distance_mi": r.distance_mi,
                        "in_region_only_mi": round(float(d_in[j] / METERS_PER_MILE), 2),
                        "ok": bool(cand["OBJECTID"].iloc[idx_all[j]] == r.nearest_terminal_objectid)})
        log(f"    {r.cell_id}: {r.distance_mi:6.2f} mi to {r.nearest_terminal}; "
            f"in-region only would be {d_in[j] / METERS_PER_MILE:6.2f} mi")

    record_check(
        "2_border_out_of_region_eligible", recheck_mismatch == 0 and n_out_closer > 0
        and bool(nearest_out[sample][overstated > DIST_TOL_MI].all()),
        f"{len(border):,} pieces within {QA_BORDER_M / 1000:.0f} km of the boundary; "
        f"{int(nearest_out[border].sum()):,} of them use an out-of-region terminal; "
        f"brute-force recheck of {len(sample)}: {recheck_mismatch} mismatches",
        all_pieces_nearest_out_of_region=int(nearest_out.sum()),
        sampled_pieces_where_out_of_region_is_closer=n_out_closer,
        max_overstatement_if_region_only_mi=round(float(overstated.max()), 2),
    )


def check_urban_exclusion(rural, urban) -> None:
    """3. Rural polygons must not overlap Census urban areas."""
    rg, ug = rural.geometry.values, urban.geometry.values
    r_idx, u_idx = shapely.STRtree(ug).query(rg, predicate="intersects")
    overlap = shapely.area(shapely.intersection(rg[r_idx], ug[u_idx])) if len(r_idx) else np.array([0.0])
    # Same deterministic sample point, taken in the working CRS (no reprojection noise).
    pts = rural.geometry.representative_point().values
    pts_in_urban = len(np.unique(shapely.STRtree(ug).query(pts, predicate="within")[0]))
    tol_m2 = 1.0   # floating-point residue along shared edges
    bad = int((overlap > tol_m2).sum())
    record_check("3_urban_exclusion", bad == 0 and pts_in_urban == 0,
                 f"{len(r_idx):,} rural/urban pairs touch; {bad} overlap by more than {tol_m2} m2 "
                 f"(max {overlap.max():.4f} m2); {pts_in_urban} sample points inside urban areas",
                 touching_pairs=int(len(r_idx)), max_overlap_m2=float(overlap.max()),
                 total_overlap_m2=float(overlap.sum()), sample_points_inside_urban=pts_in_urban)


def check_exported_numeric(path: Path) -> None:
    """4. distance_mi in the exported GeoJSON must be a JSON number, not text."""
    feats = json.loads(path.read_text(encoding="utf-8"))["features"]
    vals = [f["properties"].get("distance_mi") for f in feats]
    non_numeric = [v for v in vals if isinstance(v, bool) or not isinstance(v, (int, float))]
    non_finite = [v for v in vals if isinstance(v, (int, float)) and not np.isfinite(v)]
    dtype = str(gpd.read_file(path)["distance_mi"].dtype)
    record_check("4_distance_mi_numeric", not non_numeric and not non_finite and dtype.startswith("float"),
                 f"{len(vals):,} features; {len(non_numeric)} non-numeric, {len(non_finite)} non-finite; "
                 f"re-read dtype {dtype}",
                 json_value_types=sorted({type(v).__name__ for v in vals}), reread_dtype=dtype)


def check_most_distant(rural, terminals, study_states, samples: list) -> None:
    """5. Inspect the most distant pieces for missing facilities or bad coordinates."""
    top = rural.nlargest(QA_TOP_DISTANT_N, "distance_mi")
    pts = gpd.GeoDataFrame(top[["cell_id"]], crs=OUTPUT_CRS,
                           geometry=shapely.points(top.sample_lon, top.sample_lat))
    st = gpd.sjoin(pts.to_crs(GRID_CRS), study_states[["STUSPS", "geometry"]], how="left",
                   predicate="within").drop_duplicates("cell_id").set_index("cell_id")["STUSPS"]

    excluded = terminals[~terminals["candidate"]]
    misplaced = terminals[terminals["km_outside_stated_state"] > STATE_MISMATCH_TOL_M / 1000]
    suspicious_near = 0
    for _, r in top.iterrows():
        # Any excluded or misplaced terminal record that would be closer than the current nearest?
        notes = []
        for label, pool in [("excluded record", excluded), ("misplaced coordinates", misplaced)]:
            pool = pool[pool.geometry.notna() & ~pool.geometry.is_empty]
            if len(pool):
                d = geodesic_m(np.full(len(pool), r.sample_lon), np.full(len(pool), r.sample_lat),
                               pool.geometry.x, pool.geometry.y) / METERS_PER_MILE
                close = pool[d < r.distance_mi]
                notes += [f"{label}: {n}" for n in close["display_name"]]
        suspicious_near += bool(notes)
        nearest = terminals.loc[terminals["OBJECTID"] == r.nearest_terminal_objectid].iloc[0]
        samples.append({"check": "5_most_distant", "cell_id": r.cell_id, "sample_lon": r.sample_lon,
                        "sample_lat": r.sample_lat, "state": st.get(r.cell_id),
                        "nearest_terminal": r.nearest_terminal, "distance_mi": r.distance_mi,
                        "nearest_km_outside_stated_state": nearest["km_outside_stated_state"],
                        "notes": "; ".join(notes), "ok": not notes})
    regions = top.assign(state=top.cell_id.map(st)).groupby("state")["distance_mi"].agg(["count", "max"])
    for s, row in regions.iterrows():
        log(f"    {s}: {int(row['count'])} of top {QA_TOP_DISTANT_N}, max {row['max']:.1f} mi")
    log(f"    nearest terminals for these pieces: {', '.join(sorted(top.nearest_terminal.unique()))}")
    record_check("5_most_distant_review", suspicious_near == 0 and len(misplaced) == 0,
                 f"top {QA_TOP_DISTANT_N} pieces ({top.distance_mi.min():.1f}-{top.distance_mi.max():.1f} mi); "
                 f"{len(excluded)} excluded and {len(misplaced)} misplaced terminal records nationally; "
                 f"{suspicious_near} distant pieces would be closer to one of them",
                 states_of_top_pieces=regions["count"].astype(int).to_dict(),
                 manual_review="Confirm against current BTS/railroad sources that no active TOFC/COFC "
                               "facility serves these areas; source inclusion is not independently verified.")


def measure_within_cell_variation(rural, cand, rng) -> None:
    """Quantify how much the nearest-terminal distance varies inside a full 5-km cell."""
    full = np.flatnonzero(rural.geometry.area.to_numpy() > 0.999 * CELL_SIZE_M ** 2)
    pick = rng.choice(full, size=min(QA_VARIATION_N, len(full)), replace=False)
    sub = rural.iloc[pick]
    corners, owner = shapely.get_coordinates(sub.geometry.exterior.values, return_index=True)
    cpts = gpd.GeoSeries(shapely.points(corners), crs=GRID_CRS).to_crs(OUTPUT_CRS)
    _, d = geodesic_nearest(cpts.x, cpts.y, cand.geometry.x, cand.geometry.y)
    dev = np.abs(d / METERS_PER_MILE - sub["distance_mi"].to_numpy()[owner])
    per_cell = pd.Series(dev).groupby(owner).max()
    bound_mi = CELL_SIZE_M * np.sqrt(2) / METERS_PER_MILE
    qa["within_cell_variation"] = {
        "sampled_full_cells": int(len(sub)),
        "median_max_corner_deviation_mi": round(float(per_cell.median()), 2),
        "p95_max_corner_deviation_mi": round(float(per_cell.quantile(0.95)), 2),
        "max_corner_deviation_mi": round(float(per_cell.max()), 2),
        "theoretical_bound_mi": round(float(bound_mi), 2),
        "note": "Distance is evaluated at one sample point per polygon. Nearest-terminal distance changes "
                "by at most the distance moved, so within a 5-km cell it can differ from the reported "
                f"value by up to the cell diagonal ({bound_mi:.1f} mi). Acceptable for an exploratory map.",
    }
    v = qa["within_cell_variation"]
    log(f"  Within-cell variation (corners vs sample point, {len(sub)} full cells): "
        f"median {v['median_max_corner_deviation_mi']} mi, p95 {v['p95_max_corner_deviation_mi']} mi, "
        f"max {v['max_corner_deviation_mi']} mi (bound {v['theoretical_bound_mi']} mi)")


def validate(rural, terminals, study, urban, seed: int | None = None) -> list:
    section("6b. Validation checks")
    if seed is None:
        seed = int(np.random.SeedSequence().entropy % 2**32)
    qa["validation_seed"] = seed
    log(f"  Random seed: {seed}  (reproduce with: python prepare_rural_access.py --seed {seed})")
    rng = np.random.default_rng(seed)
    cand = terminals[terminals["candidate"]].sort_values("OBJECTID").reset_index(drop=True)
    states = gpd.read_file(f"zip://{STATES_SRC}")
    study_states = states[states["STUSPS"].isin(STUDY_STATES)].to_crs(GRID_CRS)
    samples: list = []
    log("  Check 1: pieces near known terminals")
    check_near_terminals(rural, cand, rng, samples)
    log("  Check 2: pieces near the study boundary")
    check_border_eligibility(rural, cand, study, rng, samples)
    log("  Check 3: urban exclusion")
    check_urban_exclusion(rural, urban)
    log("  Check 5: most distant pieces")
    check_most_distant(rural, terminals, study_states, samples)
    measure_within_cell_variation(rural, cand, rng)
    return samples


# --------------------------------------------------------------------------
# 7. Export
# --------------------------------------------------------------------------
def snap_polygons(gdf: gpd.GeoDataFrame, name: str) -> gpd.GeoDataFrame:
    """Snap to the export precision grid and repair, so the written file is valid.

    Plain coordinate rounding at write time can collapse slivers into lines or
    points and make polygons invalid. Snapping first keeps topology valid;
    features that collapse are recorded in the QA report.
    """
    gdf = gdf.to_crs(OUTPUT_CRS).copy()
    snapped = shapely.make_valid(shapely.set_precision(gdf.geometry.values, 10.0 ** -COORD_PRECISION))
    gdf["geometry"] = polygons_only(snapped)
    collapsed = gdf.geometry.isna() | gdf.geometry.is_empty
    id_col = "cell_id" if "cell_id" in gdf.columns else gdf.columns[0]
    qa.setdefault("export_precision", {})[name] = {
        "grid_degrees": 10.0 ** -COORD_PRECISION,
        "collapsed_features_removed": int(collapsed.sum()),
        "collapsed_ids": gdf.loc[collapsed, id_col].astype(str).tolist(),
        "multipart_after_snap": int((gdf.geom_type == "MultiPolygon").sum()),
        "invalid_after_snap": int((~gdf.loc[~collapsed].geometry.is_valid).sum()),
    }
    log(f"  {name}: snapped to 1e-{COORD_PRECISION} deg; {int(collapsed.sum())} collapsed features removed")
    return gdf[~collapsed]


def write_geojson(gdf: gpd.GeoDataFrame, name: str) -> Path:
    path = OUTPUTS / name
    if gdf.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        gdf = snap_polygons(gdf, name)
    gdf.to_crs(OUTPUT_CRS).to_file(path, driver="GeoJSON", engine="pyogrio",
                                   layer_options={"RFC7946": "YES",
                                                  "COORDINATE_PRECISION": str(COORD_PRECISION)})
    size_mb = path.stat().st_size / 1e6
    log(f"  {name}: {len(gdf):,} features, {size_mb:.1f} MB")
    return path


def export(rural, urban, study, terminals, issues, input_report) -> None:
    section("7. Export (EPSG:4326)")
    OUTPUTS.mkdir(exist_ok=True)

    write_geojson(rural[["cell_id", "nearest_terminal", "nearest_terminal_objectid",
                         "distance_mi", "distance_band", "geometry"]], "rural_access.geojson")

    urban_clip = urban[["UACE20", "NAME20", "geometry"]].copy()
    urban_clip["geometry"] = polygons_only(shapely.intersection(urban_clip.geometry.values, study.geometry.iloc[0]))
    urban_clip = urban_clip[urban_clip.geometry.notna() & ~urban_clip.geometry.is_empty]
    write_geojson(urban_clip, "urban_areas.geojson")

    # Map extent: study area plus a display buffer, and every terminal that is
    # nearest to at least one rural piece (so every referenced name is on the map).
    extent = study.buffer(TERMINAL_DISPLAY_BUFFER_M).to_crs(OUTPUT_CRS).geometry.iloc[0]
    used = set(rural["nearest_terminal_objectid"])
    cand = terminals[terminals["candidate"]]
    show = cand[cand.within(extent) | cand["OBJECTID"].isin(used)].copy()
    show["is_nearest_to_any_cell"] = show["OBJECTID"].isin(used)
    show["in_study_states"] = show["STATE"].isin(STUDY_STATES)
    show = show.rename(columns={"display_name": "terminal_label"})
    keep = ["OBJECTID", "TERMINAL", "terminal_label", "TERM_ADDRESS", "CITY", "STATE", "RAIL_CO",
            "EQUIPMENT", "is_nearest_to_any_cell", "in_study_states", "geometry"]
    write_geojson(show[[c for c in keep if c in show.columns]], "terminals.geojson")
    qa["terminals"]["exported_for_display"] = int(len(show))

    issues.to_csv(OUTPUTS / "qa_terminal_issues.csv", index=False)
    log(f"  qa_terminal_issues.csv: {len(issues)} flagged rows")

    metadata = {
        "generated_utc": qa["generated_utc"],
        "sources": {
            "state_boundaries": {**input_report["states"], "description":
                                 "U.S. Census Bureau cartographic boundary file, states, 2018, 1:500k"},
            "urban_areas": {**input_report["urban_areas"], "description":
                            "U.S. Census Bureau TIGER/Line 2024 release of 2020 Census urban areas (UAC20)"},
            "terminals": {**input_report["terminals"],
                          "published_date": "2024-07-10T00:00:00-04:00",
                          "published_date_source": "BTS catalog page (Published Date: July 10, 2024, "
                                                   "12:00:00 AM EDT); not embedded in the file",
                          "description":
                          "BTS National Transportation Atlas Database, Intermodal Freight Facilities, "
                          "Rail TOFC/COFC. file_modified is the local download timestamp. "
                          "Operating status not provided or assumed."},
        },
        "study_region": STUDY_STATES,
        "grid": {"crs": GRID_CRS, "cell_size_m": CELL_SIZE_M,
                 "origin": "snapped to multiples of cell size in EPSG:5070"},
        "rural_definition": "Grid cells clipped to the study region minus 2020 Census urban areas; "
                            "disconnected remnants exploded into separate features.",
        "distance_method": {
            "type": "Straight-line geodesic distance on the WGS84 ellipsoid (pyproj.Geod.inv)",
            "origin_point": "Interior representative point of each rural polygon (shapely representative_point)",
            "candidates": "All valid U.S. terminals nationally, including those outside the study region",
            "datum_note": "NAD83 inputs treated as coincident with WGS84 (sub-2 m difference, immaterial at mile scale)",
            "units": f"miles, metres / {METERS_PER_MILE}",
            "tie_rule": "lowest OBJECTID",
            "not_network_distance": "Distances are not road or rail network distances.",
            "within_cell_variation": qa.get("within_cell_variation", {}).get("note"),
        },
        "distance_bands": {
            "labels": BAND_LABELS,
            "edges_mi": BAND_EDGES,
            "rule": "lower bound inclusive, upper bound exclusive",
            "note": "Display categories only; not commercial viability thresholds.",
        },
        "output_crs": OUTPUT_CRS,
        "outputs": ["rural_access.geojson", "urban_areas.geojson", "terminals.geojson",
                    "qa_report.json", "qa_terminal_issues.csv", "qa_validation_samples.csv"],
    }
    (OUTPUTS / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    log("  metadata.json")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=QA_SEED,
                        help="Seed for QA sampling; omit for a fresh random sample each run.")
    args = parser.parse_args()

    input_report = inspect_inputs()
    study = load_study_area()
    grid = build_grid(study)
    urban = load_urban(study)
    rural = rural_pieces(grid, study, urban)
    terminals, issues = load_terminals()
    rural = nearest_terminals(rural, terminals)
    samples = validate(rural, terminals, study, urban, seed=args.seed)
    export(rural, urban, study, terminals, issues, input_report)

    section("7b. Post-export validation")
    check_exported_numeric(OUTPUTS / "rural_access.geojson")
    pd.DataFrame(samples).to_csv(OUTPUTS / "qa_validation_samples.csv", index=False)
    (OUTPUTS / "qa_report.json").write_text(json.dumps(qa, indent=2, default=str), encoding="utf-8")
    log("  qa_validation_samples.csv, qa_report.json written")

    failed = [k for k, v in qa["validation_checks"].items() if v["status"] != "PASS"]
    log("\nValidation: " + ("all checks passed." if not failed else f"FAILED: {', '.join(failed)}"))
    log("Done.")


if __name__ == "__main__":
    main()
