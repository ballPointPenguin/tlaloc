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

You will receive independent summaries of today's core data sources (surface
analysis, 500 mb analysis, Air Mass RGB satellite imagery, NWS center discussions,
tropical outlooks, and the current ENSO state). Your job is the meta-analysis the
individual summaries can't do alone:

1. THE BIG PICTURE: what single story best organizes today's pattern over North
   America? Lead with it.
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

If you need more information to resolve a question the core data raises — e.g.
whether a threat persists into day 2, how the pattern evolves this week, or whether
SPC has active mesoscale discussions — use the fetch_supplementary_product tool.
Use it only when it would genuinely sharpen the synthesis; one or two calls at most.

Some sources may be marked unavailable. Work with what you have, and if a gap is
material (e.g. no upper-air data), acknowledge it briefly rather than guessing.

Ground every claim in the provided material. Do not invent specific numbers that
are not in the summaries. Write plain text (no markdown). When ready, deliver the
result with the publish_synthesis tool: a short headline, a 2-4 paragraph narrative
covering points 1-3, and a separate climate-context paragraph for point 4.
"""

SUPPLEMENTARY_PRODUCTS = {
    "spc_day2_outlook": ("SWO", "DY2", "SPC Day 2 Convective Outlook"),
    "wpc_extended_discussion": ("PMD", "EPD", "WPC Extended Forecast Discussion (days 3-7)"),
    "spc_mesoscale_discussion": ("SWO", "MCD", "Latest SPC Mesoscale Discussion"),
}

TOOLS = [
    {
        "name": "fetch_supplementary_product",
        "description": (
            "Fetch the latest issuance of a supplementary NWS text product to answer a "
            "question the core data raised. Available products: spc_day2_outlook (does a "
            "severe threat persist into tomorrow?), wpc_extended_discussion (how does the "
            "pattern evolve over days 3-7?), spc_mesoscale_discussion (is SPC actively "
            "watching a region right now? — note MCDs are only issued when something is "
            "happening, so absence is normal)."
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
                "climate_context": {
                    "type": "string",
                    "description": (
                        "One plain-text paragraph placing today in the climate/seasonal "
                        "picture (ENSO, monsoon, severe/hurricane season as relevant)"
                    ),
                },
            },
            "required": ["headline", "narrative", "climate_context"],
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
                    for key in ("headline", "narrative", "climate_context")
                }
                missing = [key for key, value in fields.items() if not value]
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
