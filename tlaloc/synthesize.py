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
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

import anthropic

from .config import SYNTHESIS_EFFORT, SYNTHESIS_MODEL
from .fetching import SourceError
from .sources import SourceReport, fetch_nws_product_text

SYSTEM = """\
You are a senior synoptic meteorologist writing the daily North America pattern
note for Tlaloc, a page read by an educated audience of weather enthusiasts —
people who know what a negatively tilted trough is but don't have time to read six
charts and four discussions themselves. Write the way a forecaster briefs a
colleague at shift change: lead with the answer and skip the scene-setting. Be
tight, not terse: for most readers this note is the whole product, so it should
stand on its own.

You will receive independent summaries of today's core data sources (US and
Canadian surface analyses, 500 mb analysis, Air Mass RGB satellite imagery over
both the CONUS and the full disk, NWS center discussions and outlooks, CPC
extended-range outlooks including ensemble-mean 500 mb height anomalies at 6-10
and 8-14 day leads, daily teleconnection indices, tropical outlooks, and the
current ENSO state). Your value is the synthesis the individual summaries can't
do alone, but most readers will read only your note and never scroll to the source
summaries below it. So restating the key specifics from them is welcome — storm
names and intensities, where a system is headed, rainfall, severe, heat and cold
threats with their rough magnitudes and timing, and the few pressures, heights or
anomalies that anchor the pattern. What to cut is padding and repetition within
your own note, not detail the reader would otherwise miss.

LENGTH IS A HARD BUDGET, not a suggestion:
- headline: at most 18 words. A headline, not a sentence with clauses.
- narrative: about 350 words in three short paragraphs.
    Paragraph 1 — what matters today: the one story that best organizes the
    pattern over North America (CONUS, Canada, Mexico, and adjacent waters), and
    where the centers of action are (cyclogenesis, severe or heavy-rain threats,
    heat or cold, tropical systems). Canada and Mexico count equally when the data
    shows action there. Say plainly when there is no severe threat.
    Paragraph 2 — the upper-air setup behind it: the 500 mb pattern and the
    airmass and jet structure, and why they matter for what happens next.
    Paragraph 3 — the days ahead: how the pattern is expected to evolve over the
    next 3-7 days and where the next hazards are, including the regime question
    below when it applies.
- regional_notes: at most 80 words, or empty. Sub-synoptic signals a regional
  reader would want (active SPC mesoscale discussions, localized flood or heat
  threats, notable Canadian or Mexican detail) that don't belong in the narrative.
  Leave it empty when nothing rises above the synoptic story.
- climate_context: about 100 words. What explains or frames today's weather (ENSO
  phase and trend, monsoon, severe or hurricane season), with the current values
  that matter. On a quiet day, shorter is fine; do not pad.

Priorities within that budget:
- Change over time. Recent Tlaloc analyses may be appended. When today continues,
  intensifies, or breaks from them, say so in a clause ("the cutoff low, now in
  its third day..."). Do not force continuity remarks when the pattern has simply
  reset, and never treat a prior analysis as a source for today's specifics.
- Regime change vs. temporary flattening. Mention this only when a blocking or
  otherwise persistent regime is in place and something looks poised to disrupt
  it, and then in a sentence or two: one shortwave, front, or cool-down is not a
  regime change. Test it against the 6-10 and 8-14 day height anomaly charts. A
  positive anomaly that rebuilds in the same place at the longer lead means the
  disruption is cosmetic; one that shrinks or whose core retreats (often toward
  the Southwest and northern Mexico) means the regime may be transitioning. Say
  plainly when the data cannot settle it.

The core data includes any SPC mesoscale discussions active in the last few hours
(or an explicit note that none are). If you need more information to resolve a
question the core data raises — e.g. whether a threat persists into day 2, or how
the pattern evolves this week — use the fetch_supplementary_product tool, only
when it would genuinely sharpen the note, one or two calls at most.

Some sources may be marked unavailable. Work with what you have, and if a gap is
material (e.g. no upper-air data), acknowledge it in a clause rather than guessing.

DIAGNOSTIC DISCIPLINE. Your readers know the vocabulary, which means they will
notice when it is used loosely. Brevity is no excuse for a nearly-right label.

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
- Say what supports a claim. Satellite imagery shows dry air; it does not by itself
  establish descent. Broad dry air under a ridge is low-PV, subsident air, not a
  stratospheric intrusion: reserve "high-PV" for compact features on the cyclonic
  side of the flow. An airmass boundary implies a jet corridor; it does not locate
  a jet axis. Teleconnection indices are supplementary and lag the height field —
  a cross-check on a story the height and PV fields already tell, never the basis
  for one.

Ground every claim in the provided material. Do not invent specific numbers that
are not in the summaries. Write plain text (no markdown). When ready, deliver the
result with the publish_synthesis tool.
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
                    "description": "Plain-text headline of at most 18 words capturing today's pattern story",
                },
                "narrative": {
                    "type": "string",
                    "description": (
                        "About 350 words in three short plain-text paragraphs separated by "
                        "blank lines: what matters today and where the action is; the "
                        "upper-air setup behind it; and the days ahead (3-7 days)"
                    ),
                },
                "regional_notes": {
                    "type": "string",
                    "description": (
                        "At most 80 words of plain text on sub-synoptic regional signals "
                        "that don't fit the main narrative: active SPC mesoscale "
                        "discussions and watches, localized flood or heat threats, "
                        "notable regional detail in Canada or Mexico. Use an empty "
                        "string when nothing rises above the synoptic narrative today."
                    ),
                },
                "climate_context": {
                    "type": "string",
                    "description": (
                        "About 100 words of plain text placing today in the climate/seasonal "
                        "picture (ENSO, monsoon, severe/hurricane season) — what frames "
                        "today's weather, with the values that matter; shorter on a quiet day"
                    ),
                },
            },
            "required": ["headline", "narrative", "regional_notes", "climate_context"],
            "additionalProperties": False,
        },
    },
]

MAX_TURNS = 8
# Hard word limits enforced on publish_synthesis. They sit a little above the
# targets in SYSTEM so that a slight overshoot is accepted; a larger one is sent
# back for tightening. After MAX_LENGTH_REJECTIONS the draft is published anyway,
# since a long note beats no note.
WORD_LIMITS = {
    "headline": 25,
    "narrative": 480,
    "regional_notes": 110,
    "climate_context": 140,
}
MAX_LENGTH_REJECTIONS = 2


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


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", " ".join(text.split()), maxsplit=1)[0]


def build_history_block(history_records: list[dict], today: date) -> str:
    """Pattern-continuity context: headlines, plus yesterday's opening sentence.

    The headline trail sketches the trajectory at minimal token cost, and
    yesterday's lead sentence is enough to write "the ridge noted yesterday has
    shifted east". Feeding back whole narratives anchors the model on its own
    phrasing and invites repetition.
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
            lead = _first_sentence(synthesis.get("narrative", ""))
            if lead:
                lines.append(lead)
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
    length_rejections = 0

    for _ in range(MAX_TURNS):
        # Streamed because the SDK refuses non-streaming calls whose max_tokens
        # could imply a >10 minute request; we only need the assembled message.
        with client.messages.stream(
            model=SYNTHESIS_MODEL,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": SYNTHESIS_EFFORT},
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

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
                over = {
                    key: len(fields[key].split())
                    for key, limit in WORD_LIMITS.items()
                    if len(fields[key].split()) > limit
                }
                if over and length_rejections < MAX_LENGTH_REJECTIONS:
                    length_rejections += 1
                    detail = ", ".join(
                        f"{key} is {count} words (limit {WORD_LIMITS[key]})"
                        for key, count in over.items()
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"Rejected as too long: {detail}. Cut to the budget by dropping "
                            "repetition and padding, then call "
                            "publish_synthesis again."
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
