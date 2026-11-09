# Map styling

The finished map was styled in Mapbox Studio. This folder holds the exported
style, sanitized for publication, and a record of the tilesets it depends on.

> **Status:** the sanitizer and dependency notes are in place; `style.json` and
> `TILESETS.md` will be added from the Mapbox Studio export.

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

| Layer content | Pipeline output | Key attributes |
|---|---|---|
| Rural distance polygons | `rural_access.geojson` | `cell_id`, `nearest_terminal`, `distance_mi`, `distance_band` |
| Urban areas (excluded land) | `urban_areas.geojson` | `UACE20`, `NAME20` |
| Terminals | `terminals.geojson` | `OBJECTID`, `terminal_label`, `RAIL_CO`, `in_study_states` |

## Regenerating

```
python map_style/sanitize_style.py path/to/mapbox-studio-export.zip
```
