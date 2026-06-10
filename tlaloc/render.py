"""Render the analysis into index.html between the TLALOC CONTENT sentinels."""

import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .sources import SourceReport
from .synthesize import Synthesis

SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN TLALOC CONTENT -->).*?([ \t]*<!-- END TLALOC CONTENT -->)",
    re.DOTALL,
)


def format_timestamp(dt: datetime) -> tuple[str, str]:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    iso = dt.isoformat().replace("+00:00", "Z")
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return iso, f"{dt.strftime('%B')} {dt.day}, {dt.year} at {time_text} UTC"


def paragraphs(text: str, css_class: str) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n".join(
        f'      <p class="{css_class}">{escape(" ".join(p.split()))}</p>' for p in parts
    )


def render_synthesis_section(synthesis: Synthesis, generated_at: datetime) -> str:
    iso, human = format_timestamp(generated_at)
    return f"""<section aria-labelledby="synthesis-heading">
  <h2 id="synthesis-heading">Today&rsquo;s Synoptic Picture</h2>
  <div class="synthesis-card">
    <p class="synthesis-card__headline">{escape(synthesis.headline)}</p>
{paragraphs(synthesis.narrative, "synthesis-card__text")}
    <h3 class="synthesis-card__subheading">Climate Context</h3>
{paragraphs(synthesis.climate_context, "synthesis-card__text")}
    <p class="synoptic-card__timestamp">
      <small>Synthesized: <time datetime="{iso}">{human}</time></small>
    </p>
  </div>
</section>"""


def render_image_section(report: SourceReport) -> str:
    summary = escape(" ".join((report.summary or "").split()))
    return f"""<section aria-labelledby="{report.key}-heading">
  <h2 id="{report.key}-heading">{escape(report.title)}</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{escape(report.display_url or '', quote=True)}"
      alt="{escape(f'{report.credit} {report.title} chart', quote=True)}"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{summary}</p>
      <p class="synoptic-card__timestamp"><small>{escape(report.credit)}</small></p>
    </div>
  </div>
</section>"""


def render_source_notes(reports: list[SourceReport]) -> str:
    items = []
    for report in reports:
        if report.status == "ok" and report.kind == "text" and report.summary:
            items.append(
                f"""    <li class="source-note">
      <span class="source-note__title">{escape(report.title)}</span>
      <span class="source-note__body">{escape(' '.join(report.summary.split()))}</span>
    </li>"""
            )
        elif report.status != "ok":
            items.append(
                f"""    <li class="source-note source-note--failed">
      <span class="source-note__title">{escape(report.title)}</span>
      <span class="source-note__body">Unavailable for this analysis.</span>
    </li>"""
            )
    if not items:
        return ""
    body = "\n".join(items)
    return f"""<section aria-labelledby="sources-heading">
  <h2 id="sources-heading">Sources Consulted</h2>
  <ul class="source-notes">
{body}
  </ul>
</section>"""


def render_content(
    synthesis: Synthesis, reports: list[SourceReport], generated_at: datetime
) -> str:
    sections = [render_synthesis_section(synthesis, generated_at)]
    for report in reports:
        if report.kind == "image" and report.status == "ok":
            sections.append(render_image_section(report))
    notes = render_source_notes(reports)
    if notes:
        sections.append(notes)
    return "\n\n".join(sections)


def write_index_html(
    index_path: Path,
    synthesis: Synthesis,
    reports: list[SourceReport],
    generated_at: datetime | None = None,
) -> None:
    generated_at = generated_at or datetime.now(timezone.utc)
    source = index_path.read_text()
    content = render_content(synthesis, reports, generated_at)
    replacement = (
        "        <!-- BEGIN TLALOC CONTENT -->\n"
        f"{content}\n"
        "        <!-- END TLALOC CONTENT -->"
    )
    # Use a callable replacement so backslashes in content aren't treated as
    # regex group references.
    updated, count = SENTINEL_RE.subn(lambda _match: replacement, source)
    if count == 0:
        raise RuntimeError(f"TLALOC CONTENT sentinel comments not found in {index_path}")
    index_path.write_text(updated)
