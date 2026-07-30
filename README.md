# tlaloc

Daily synoptic pattern analysis for North America, named for Tláloc, the Mexica
deity of water, rain, fertility, and storms. Published to GitHub Pages.

Anyone can get a point forecast anywhere. Tlaloc instead answers the questions an
educated weather enthusiast actually asks each day: *What is the big picture? Where
are the focal points? What's happening in the upper atmosphere that matters? How
does today fit into the climate moment (ENSO, the monsoon, the severe/hurricane
seasons)?*

## How it works

A pipeline (`tlaloc/`):

```
collect  ->  interpret (one Claude call per source)  ->  synthesize (one meta call)  ->  render + archive
```

**Stage 1 — fixed core sources.** Every run fetches the same backbone of open data:

| Source | Kind | Provider |
| --- | --- | --- |
| Surface analysis | chart | NOAA WPC |
| Canadian surface analysis | chart | Environment and Climate Change Canada |
| 500 mb heights/winds | chart | College of DuPage NEXLAB |
| Air Mass RGB (CONUS) | satellite | NOAA NESDIS / GOES East |
| Air Mass RGB (Full Disk: Canada, Mexico, tropics) | satellite | NOAA NESDIS / GOES East |
| 6-10 Day Temperature Outlook | chart | NOAA CPC |
| 6-10 Day 500 mb Height Outlook (ensemble-mean anomalies) | chart | NOAA CPC |
| 8-14 Day 500 mb Height Outlook (ensemble-mean anomalies) | chart | NOAA CPC |
| Short Range Forecast Discussion | text | NOAA WPC via api.weather.gov |
| Day 1 Convective Outlook | text | NOAA SPC via api.weather.gov |
| Mesoscale Discussions (last 6 h; absence noted) | text | NOAA SPC via api.weather.gov |
| Tropical Weather Outlooks (ATL + EPAC) | text | NOAA NHC |
| Oceanic Niño Index table | data | NOAA CPC |
| Daily teleconnection indices (PNA, AO, NAO) | data | NOAA CPC |

Each chart gets an independent vision interpretation; each text product gets a short
distillation. These per-source summaries are the building blocks. (A stable
Mexico-specific product from SMN/CONAGUA is a future candidate; for now Mexico is
covered by the Full Disk satellite frame, the NHC East Pacific outlook, and the
surface analyses.)

**Stage 2 — meta-synthesis.** All summaries (including notes about any sources that
failed) go to a single synthesis call that writes the day's pattern discussion:
big picture, focal points, upper-level context, regional signals, and climate
context. The briefing also includes Tlaloc's own recent analyses — yesterday's in
full, older headlines — so the write-up can describe pattern evolution ("the cutoff
low, now in its third day..."). The synthesist also has a small tool menu of
*supplementary* products it may fetch on demand if the core data raises questions —
the SPC Day 2 outlook or the WPC extended discussion.

**Stage 3 — render + archive.** The synthesis rewrites `index.html`, and each run
also writes a per-day JSON record (`data/YYYY-MM-DD.json`) — the durable, text-only
history — from which a standalone archive page (`archive/YYYY-MM-DD.html`) and the
archive index are derived. Chart images are deliberately not archived: their source
URLs are overwritten or expire within days, and the interpretation text already
records what each chart showed. A hand-maintained `glossary.html` explains the
jargon.

### Design decisions

- **Two lead times of height anomalies, on purpose.** Everything else in the backbone
  is an analysis or a short-range forecast, which can say a front is coming but not
  whether the pattern it interrupts comes back. The 6-10 and 8-14 day ensemble-mean
  500 mb height outlooks are the pair that answers that: a positive anomaly that
  rebuilds in the same place at the longer lead means the block was dented, not
  broken. The synthesis prompt asks for that comparison explicitly whenever a
  persistent regime is in play, since "one cold front" and "regime change" are
  routinely confused. Teleconnection indices (PNA especially) are a cheap
  quantitative cross-check on the same question, and are treated as supplementary —
  they lag the height field and never carry a conclusion alone.
- **Prompts enforce diagnostic discipline, not just topics.** The interpretation and
  synthesis prompts carry explicit rules about what each product can support:
  absolute values are not anomalies, Air Mass RGB shows dry high-PV air rather than
  proving descent, an airmass boundary implies a jet corridor rather than locating a
  jet axis, named classifications (Rex block, omega block) require their actual
  geometry, and a 1018 mb summer high does not explain convective suppression. These
  are the failure modes a fluent model falls into unprompted — reaching for the
  precise-sounding term that is slightly wrong.
- **Core sources are hardcoded; the menu is for follow-ups.** A daily overview needs
  the same backbone every day, so no model decides whether to fetch the surface
  analysis. Dynamic source selection lives in the synthesis stage, where a question
  raised by the data ("does this severe threat persist into day 2?") can justify an
  extra fetch.
- **Soft failure everywhere.** Timestamped chart URLs are probed with a lookback
  window; charts whose filename isn't stable are found by reading the product page
  and following its own `<img>` references, with hardcoded URLs as a fallback and a
  failure message that names what the page did offer, so a `--collect-only` run
  diagnoses an upstream change rather than merely reporting one; content types and
  sizes are validated; every collector converts errors
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

```sh
# Lint and unit tests
uv run ruff check .
uv run pytest
```

The GitHub Actions workflow (`.github/workflows/update-weather.yml`) runs the full
pipeline daily at 13:30 UTC (after the 12Z upper-air analyses publish) and commits
the updated `index.html`, `data/`, and `archive/`. Pull requests run lint, tests,
and `--collect-only` as a smoke test of the data sources.

## Adding a source

Add a collector in `tlaloc/sources.py` that returns a `SourceReport`, register it in
`COLLECTORS`, and (for charts) add an analytical focus prompt in
`tlaloc/interpret.py`. Failures are handled for you as long as the collector raises
`SourceError` (or returns `report.fail(...)`). Run `--collect-only` before opening a
PR: it is the only check that a new URL is actually live.

Wanted but not yet added, for lack of a stable public still-image endpoint: a 500 mb
height-anomaly Hovmöller (the cleanest single test of whether a wave is phase-locked
or progressing), ensemble spaghetti and cluster plots, and dynamic-tropopause/PV
maps. The extended-range height outlooks above are the closest available substitute.
