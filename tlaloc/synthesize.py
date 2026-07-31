"""Meta-synthesis: one Claude call that sees every source summary at once.

This is where the dynamic "menu" lives. The core sources are fixed (see
sources.py), but the synthesist can pull supplementary products on demand —
the SPC Day 2 outlook, the WPC extended discussion, or active SPC mesoscale
discussions — when the core data raises questions worth chasing. A failed
supplementary fetch is returned to the model as a tool error, and it simply
synthesizes without it.

The final write-up is delivered through the publish_synthesis tool so the
output arrives structured (headline / narrative / climate context) instead of
needing to be parsed out of prose.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import anthropic

from .config import SYNTHESIS_MODEL
from .fetching import SourceError
from .sources import SourceReport, fetch_nws_product_text

SYSTEM = """\
You are a senior synoptic meteorologist writing the daily North America pattern
discussion for Tlaloc, a page read by an educated audience of weather enthusiasts —
people who know what a negatively tilted trough is but don't have time to read six
charts and four discussions themselves.

You will receive independent summaries of today's core data sources (US and
Canadian surface analyses, 500 mb analysis, Air Mass RGB satellite imagery over
both the CONUS and the full disk, NWS center discussions and outlooks, CPC
extended-range outlooks including ensemble-mean 500 mb height anomalies at 6-10
and 8-14 day leads, daily teleconnection indices, tropical outlooks, and the
current ENSO state). Your job is the meta-analysis the individual summaries can't
do alone:

1. THE BIG PICTURE: what single story best organizes today's pattern over North
   America — the CONUS, Canada, Mexico, and adjacent waters? Lead with it.
   Canada and Mexico are part of the story, not scenery: when the data shows
   action there, give it the same weight as a comparable feature over the US.
2. FOCAL POINTS: where are today's centers of action — developing cyclogenesis,
   severe weather threats, heavy rain axes, heat domes, tropical mischief?
3. UPPER-LEVEL CONTEXT: connect the surface story to the 500 mb pattern and the
   airmass/jet structure. Say why the upper pattern matters for what happens next.
4. CLIMATE CONTEXT: place today inside the seasonal and climate picture — the
   current ENSO state (an ONI table is provided), and seasonally relevant regimes
   such as the North American Monsoon, severe-weather season, or hurricane season,
   as appropriate for the date.
5. PATTERN EVOLUTION: recent Tlaloc analyses may be appended to the briefing.
   Where today's pattern continues, intensifies, or breaks from what was described
   there, say so explicitly ("the cutoff low over Texas, now in its third day...").
   Do not force continuity remarks when the pattern has simply reset, and never
   treat a prior analysis as a data source for today's specifics — today's claims
   come from today's sources.
6. REGIME CHANGE VS. TEMPORARY FLATTENING: when a blocking or otherwise persistent
   regime is in place and something appears poised to disrupt it, the interesting
   question is not whether the disturbance arrives but whether the regime
   regenerates behind it. One shortwave, one cold front, or one cool-down is not a
   regime change. Test the claim against the ensemble-mean height anomaly outlooks:
   if the positive anomaly rebuilds in the same place at the longer lead, the
   disruption is cosmetic; if it keeps shrinking or the ridge core retreats, the
   regime may genuinely be transitioning. Compare the 6-10 and 8-14 day height
   charts against each other explicitly when both are available — that pair is the
   single best test you have — and note that a ridge often contracts geographically
   (retreating toward the Southwest and northern Mexico while the north cools)
   rather than disappearing. Say which of these is happening, and say plainly when
   the available data cannot settle it.

The core data includes any SPC mesoscale discussions active in the last few hours
(or an explicit note that none are). Use them, plus any localized threats the other
sources flag, to fill the regional_notes field of your write-up: the sub-synoptic
signals a regional reader would want that don't belong in the main narrative. Leave
regional_notes empty when nothing rises above the synoptic story.

If you need more information to resolve a question the core data raises — e.g.
whether a threat persists into day 2, or how the pattern evolves this week — use
the fetch_supplementary_product tool. Use it only when it would genuinely sharpen
the synthesis; one or two calls at most.

Some sources may be marked unavailable. Work with what you have, and if a gap is
material (e.g. no upper-air data), acknowledge it briefly rather than guessing.

DIAGNOSTIC DISCIPLINE. Your readers know the vocabulary, which means they will
notice when it is used loosely. Hold yourself to these:

- Named classifications are definitions, not flavor. A Rex block, for instance, is
  an anticyclone stacked poleward of a cutoff cyclone at roughly the same longitude.
  When the summaries don't describe that, describe the geometry in your own words
  rather than reaching for the nearest label.
- Distinguish absolute values from departures from normal. A 500 mb temperature,
  height, or pressure taken off an analysis is an absolute value: call it
  unseasonably cool or unusually high, and reserve "anomalous" for the products
  that actually plot anomalies, percentiles, or departures from normal.
- Attribute effects to the right mechanism. Convective suppression under a heat
  dome comes from midlevel subsidence and warming, a strengthened cap, dry-air
  entrainment, and weak large-scale ascent — not from a modest surface high. A
  1018 mb summer ridge is not a strong feature and should not be asked to carry an
  explanation on its own.
- Say what supports a claim. Satellite imagery shows dry, ozone-rich, high-PV air;
  it does not by itself establish descent. An airmass boundary implies a jet
  corridor; it does not locate a jet axis. Teleconnection indices are supplementary
  and lag the height field — use them as a cross-check on a story the height and
  PV fields already tell, never as the basis for one.

Ground every claim in the provided material. Do not invent specific numbers that
are not in the summaries. Write plain text (no markdown). When ready, deliver the
result with the publish_synthesis tool: a short headline, a 2-4 paragraph narrative
covering points 1-3, regional notes (possibly empty), and a separate
climate-context paragraph for point 4.
"""

SUPPLEMENTARY_PRODUCTS = {
    "spc_day2_outlook": ("SWO", "DY2", "SPC Day 2 Convective Outlook"),
    "wpc_extended_discussion": ("PMD", "EPD", "WPC Extended Forecast Discussion (days 3-7)"),
}

TOOLS = [
    {
        "name": "fetch_supplementary_product",
        "description": (
            "Fetch the latest issuance of a supplementary NWS text product to answer a "
            "question the core data raised. Available products: spc_day2_outlook (does a "
            "severe threat persist into tomorrow?), wpc_extended_discussion (how does the "
            "pattern evolve over days 3-7?)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "enum": sorted(SUPPLEMENTARY_PRODUCTS),
                    "description": "Which supplementary product to fetch",
                },
            },
            "required": ["product"],
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_synthesis",
        "description": (
            "Deliver the final synthesis for the Tlaloc page. Call this exactly once, "
            "after any supplementary fetches, with the complete write-up. All three "
            "fields are required; climate_context must be its own paragraph, separate "
            "from the narrative."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {
                    "type": "string",
                    "description": "One-line plain-text headline capturing today's pattern story",
                },
                "narrative": {
                    "type": "string",
                    "description": (
                        "2-4 plain-text paragraphs separated by blank lines: the big "
                        "picture, focal points, and upper-level context"
                    ),
                },
                "regional_notes": {
                    "type": "string",
                    "description": (
                        "One short plain-text paragraph of sub-synoptic regional signals "
                        "that don't fit the main narrative: active SPC mesoscale "
                        "discussions and watches, localized flood or heat threats, "
                        "notable regional detail in Canada or Mexico. Use an empty "
                        "string when nothing rises above the synoptic narrative today."
                    ),
                },
                "climate_context": {
                    "type": "string",
                    "description": (
                        "One plain-text paragraph placing today in the climate/seasonal "
                        "picture (ENSO, monsoon, severe/hurricane season as relevant)"
                    ),
                },
            },
            "required": ["headline", "narrative", "regional_notes", "climate_context"],
            "additionalProperties": False,
        },
    },
]

MAX_TURNS = 8


@dataclass
class Synthesis:
    headline: str
    narrative: str
    climate_context: str
    # Sub-synoptic regional signals; empty means nothing noteworthy today.
    regional_notes: str = ""


def _days_ago_label(record_date: str, today: date) -> str:
    delta = (today - date.fromisoformat(record_date)).days
    if delta == 1:
        return f"Yesterday ({record_date})"
    return f"{delta} days ago ({record_date})"


def build_history_block(history_records: list[dict], today: date) -> str:
    """Pattern-continuity context: yesterday in full, older days headline-only.

    Yesterday's complete narrative is what lets the synthesist write "the
    ridge noted yesterday has shifted east"; earlier headlines sketch the
    trajectory at minimal token cost.
    """
    lines = [
        "=== Recent Tlaloc analyses (for pattern continuity) ===",
        "These are Tlaloc's own prior write-ups, newest first. Use them for",
        "evolution and trend language only, never as a source for today's facts.",
    ]
    for i, record in enumerate(history_records):
        synthesis = record.get("synthesis", {})
        lines.append("")
        lines.append(f"{_days_ago_label(record['date'], today)}: {synthesis.get('headline', '')}")
        if i == 0:
            for field in ("narrative", "regional_notes", "climate_context"):
                text = synthesis.get(field, "").strip()
                if text:
                    lines.append(text)
    return "\n".join(lines)


def build_briefing(
    reports: list[SourceReport],
    now_utc: datetime,
    history_records: list[dict] | None = None,
) -> str:
    lines = [
        f"Date/time of this briefing: {now_utc:%A, %B %d, %Y at %H:%M UTC}",
        "",
        "Source summaries follow. Each was produced independently from a live "
        "chart or official text product.",
    ]
    for report in reports:
        lines.append("")
        lines.append(f"=== {report.title} ({report.credit}) ===")
        if report.status == "ok" and report.summary:
            lines.append(report.summary)
        else:
            lines.append(f"[UNAVAILABLE — {report.error or 'no data retrieved'}]")
    if history_records:
        lines.append("")
        lines.append(build_history_block(history_records, now_utc.date()))
    return "\n".join(lines)


def run_supplementary_fetch(product: str) -> str:
    if product not in SUPPLEMENTARY_PRODUCTS:
        raise SourceError(f"Unknown supplementary product: {product!r}")
    type_id, location, title = SUPPLEMENTARY_PRODUCTS[product]
    text = fetch_nws_product_text(type_id, location)
    return f"{title}:\n\n{text}"


def synthesize(
    client: anthropic.Anthropic,
    reports: list[SourceReport],
    history_records: list[dict] | None = None,
) -> Synthesis:
    now_utc = datetime.now(timezone.utc)
    messages = [{"role": "user", "content": build_briefing(reports, now_utc, history_records)}]
    synthesis: Synthesis | None = None

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  synthesis tool call: {block.name}({json.dumps(block.input)[:200]})")
            if block.name == "publish_synthesis":
                fields = {
                    key: str(block.input.get(key, "")).strip()
                    for key in ("headline", "narrative", "climate_context", "regional_notes")
                }
                # regional_notes is legitimately empty on quiet days.
                missing = [
                    key
                    for key, value in fields.items()
                    if not value and key != "regional_notes"
                ]
                if missing:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"Rejected: missing required field(s) {', '.join(missing)}. "
                            "Call publish_synthesis again with every field populated."
                        ),
                        "is_error": True,
                    })
                    continue
                synthesis = Synthesis(**fields)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"success": True}),
                })
            elif block.name == "fetch_supplementary_product":
                try:
                    content = run_supplementary_fetch(block.input.get("product", ""))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })
                except SourceError as exc:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Product unavailable: {exc}. Synthesize without it.",
                        "is_error": True,
                    })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool {block.name}",
                    "is_error": True,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        if synthesis is not None:
            break

    if synthesis is None:
        raise RuntimeError("Synthesis model never called publish_synthesis")
    return synthesis
