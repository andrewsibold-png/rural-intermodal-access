# Map styling

The finished map was styled in Mapbox Studio. This folder holds the exported
style, sanitized for publication, and a record of the tilesets it depends on.

## Layer design

| Layer | Type | Styling |
|---|---|---|
| `rural-access` | fill | Linear color ramp on `distance_mi` (ColorBrewer Greens): `#edf8e9` at 0, `#bae4bc` at 25, `#74c476` at 50, `#31a354` at 100, `#006d2c` at 150 miles; opacity 0.65 |
| `urban-areas` | fill | `#c3c7cc`, opacity 0.65 (excluded land) |
| `terminals` | circle | `#d4a72c` fill, 1 px `#16324f` stroke |
| `cb-2018-us-state-500k-zip-wjnxkc` | line | State boundaries, `#69757d`, 2 px, opacity 0.71 |
| `admin` | line | Country boundaries (`admin_level` 1) from Mapbox Streets, `#69757d`, 1.5 px |

The ramp is continuous: its stops coincide with the analysis distance bands
but the map shades between them rather than in discrete classes.

**Basemap.** The style imports Mapbox Standard (`mapbox://styles/mapbox/standard`)
with the `monochrome` theme, `day` light preset, 3D objects off, transit labels
off, and custom road and label colors. The configuration is in the style's
`imports` block.

## Contents

| File | Purpose |
|---|---|
| `style.json` | Mapbox GL style exported from Mapbox Studio, with access tokens and account metadata removed |
| `TILESETS.md` | Every source the style references, generated from `style.json` |
| `sanitize_style.py` | Script that produces both files from a Studio export |

## Credentials

`sanitize_style.py` removes `access_token` and similar query parameters from
every URL, drops the account fields `owner`, `created`, `modified`,
`visibility`, `draft` and `protected`, and refuses to write output if any string
resembling a Mapbox token (`pk.`, `sk.`, `tk.`) remains. To render the style you
must supply your own Mapbox public token in the client.

## External tileset dependencies

The style depends on two kinds of source.

1. **Mapbox public tilesets** (basemap streets, places, terrain), referenced as
   `mapbox://mapbox.*`. These are available to any Mapbox account.
2. **Account tilesets** created by uploading this project's GeoJSON to Mapbox
   Studio. They are owned by the author's account and are not public, so a
   style URL such as `mapbox://<account>.<tileset-id>` will not resolve for
   anyone else. To reproduce the map:
   1. Run the pipeline (see the main README) to produce
      `Outputs/rural_access.geojson`, `Outputs/urban_areas.geojson` and
      `Outputs/terminals.geojson`.
   2. Upload each file to Mapbox Studio as a tileset.
   3. Replace the account tileset IDs listed in `TILESETS.md` with your own.

| Style layer | Account tileset in `style.json` | Upload this file | Key attributes |
|---|---|---|---|
| `rural-access` | `asibold-iana.4lo30qau59io` | `Outputs/rural_access.geojson` | `cell_id`, `nearest_terminal`, `distance_mi`, `distance_band` |
| `urban-areas` | `asibold-iana.xtty9bk8zkxy` | `Outputs/urban_areas.geojson` | `UACE20`, `NAME20` |
| `terminals` | `asibold-iana.9ciqeyf9bc3x` | `Outputs/terminals.geojson` | `OBJECTID`, `terminal_label`, `RAIL_CO`, `in_study_states` |
| `cb-2018-us-state-500k-zip-wjnxkc` | `asibold-iana.0kawer` (in `composite`) | `Inputs/cb_2018_us_state_500k.zip` | `STUSPS`, `NAME` |

When you upload, Mapbox assigns new tileset IDs and source-layer names. Update
each layer's `source` and `source-layer` in `style.json` to match. The
`composite` source also includes the public `mapbox.mapbox-streets-v8`, which
needs no change.

The **sprite** URL (`mapbox://sprites/asibold-iana/...`) is account-specific.
The style draws no sprite icons, so you can point it at any sprite you own, or
let Studio replace it on import.

## Regenerating

```
python map_style/sanitize_style.py path/to/mapbox-studio-export.zip
```
