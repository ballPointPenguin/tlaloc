# tlaloc

Daily synoptic pattern analysis for North America, named for Tláloc, the Mexica
deity of water, rain, fertility, and storms. Published to GitHub Pages.

Anyone can get a point forecast anywhere. Tlaloc instead answers the questions an
educated weather enthusiast actually asks each day: *What is the big picture? Where
are the focal points? What's happening in the upper atmosphere that matters? How
does today fit into the climate moment (ENSO, the monsoon, the severe/hurricane
seasons)?*

## How it works

A two-stage pipeline (`tlaloc/`):

```
collect  ->  interpret (one Claude call per source)  ->  synthesize (one meta call)  ->  render
```

**Stage 1 — fixed core sources.** Every run fetches the same backbone of open data:

| Source | Kind | Provider |
| --- | --- | --- |
| Surface analysis | chart | NOAA WPC |
| 500 mb heights/winds | chart | College of DuPage NEXLAB |
| Air Mass RGB (CONUS) | satellite | NOAA NESDIS / GOES East |
| Short Range Forecast Discussion | text | NOAA WPC via api.weather.gov |
| Day 1 Convective Outlook | text | NOAA SPC via api.weather.gov |
| Tropical Weather Outlooks (ATL + EPAC) | text | NOAA NHC |
| Oceanic Niño Index table | data | NOAA CPC |

Each chart gets an independent vision interpretation; each text product gets a short
distillation. These per-source summaries are the building blocks.

**Stage 2 — meta-synthesis.** All summaries (including notes about any sources that
failed) go to a single synthesis call that writes the day's pattern discussion:
big picture, focal points, upper-level context, and climate context. The synthesist
also has a small tool menu of *supplementary* products it may fetch on demand if the
core data raises questions — the SPC Day 2 outlook, the WPC extended discussion, or
active SPC mesoscale discussions.

### Design decisions

- **Core sources are hardcoded; the menu is for follow-ups.** A daily overview needs
  the same backbone every day, so no model decides whether to fetch the surface
  analysis. Dynamic source selection lives in the synthesis stage, where a question
  raised by the data ("does this severe threat persist into day 2?") can justify an
  extra fetch.
- **Soft failure everywhere.** Timestamped chart URLs are probed with a lookback
  window; content types and sizes are validated; every collector converts errors
  into a `failed` report rather than raising. The synthesis prompt is told exactly
  which sources are missing so it doesn't overreach. If too few sources survive
  (fewer than 1 chart or 2 total), the run aborts *without touching the page* —
  yesterday's analysis stays up rather than publishing something thin.
- **Models.** Chart interpretation uses `claude-sonnet-4-6` (vision), text
  distillation uses `claude-haiku-4-5`, and the once-daily synthesis uses
  `claude-opus-4-8` with adaptive thinking. Override with `TLALOC_VISION_MODEL`,
  `TLALOC_TEXT_MODEL`, `TLALOC_SYNTHESIS_MODEL`.

## Running

```sh
uv sync

# Check that all data sources are reachable (no API key needed)
uv run python -m tlaloc --collect-only

# Full run: fetch, interpret, synthesize, and rewrite index.html
ANTHROPIC_API_KEY=... uv run python -m tlaloc
```

The GitHub Actions workflow (`.github/workflows/update-weather.yml`) runs the full
pipeline daily at 13:30 UTC (after the 12Z upper-air analyses publish) and commits
the updated `index.html`. Pull requests run `--collect-only` as a smoke test of the
data sources.

## Adding a source

Add a collector in `tlaloc/sources.py` that returns a `SourceReport`, register it in
`COLLECTORS`, and (for charts) add an analytical focus prompt in
`tlaloc/interpret.py`. Failures are handled for you as long as the collector raises
`SourceError` (or returns `report.fail(...)`).
