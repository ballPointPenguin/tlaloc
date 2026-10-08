"""Render the analysis into index.html between the TLALOC CONTENT sentinels,
plus the standalone archive pages derived from per-day history records."""

import re
from dataclasses import asdict
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


def _synthesis_card(fields: dict, heading: str, generated_at: datetime) -> str:
    iso, human = format_timestamp(generated_at)
    regional = ""
    if fields.get("regional_notes", "").strip():
        regional = (
            '    <h3 class="synthesis-card__subheading">Regional Signals</h3>\n'
            + paragraphs(fields["regional_notes"], "synthesis-card__text")
            + "\n"
        )
    return f"""<section aria-labelledby="synthesis-heading">
  <h2 id="synthesis-heading">{heading}</h2>
  <div class="synthesis-card">
    <p class="synthesis-card__headline">{escape(fields["headline"])}</p>
{paragraphs(fields["narrative"], "synthesis-card__text")}
{regional}    <h3 class="synthesis-card__subheading">Climate Context</h3>
{paragraphs(fields["climate_context"], "synthesis-card__text")}
    <p class="synoptic-card__timestamp">
      <small>Synthesized: <time datetime="{iso}">{human}</time></small>
    </p>
  </div>
</section>"""


def render_synthesis_section(synthesis: Synthesis, generated_at: datetime) -> str:
    return _synthesis_card(asdict(synthesis), "Today&rsquo;s Synoptic Picture", generated_at)


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


def _source_notes_section(notes: list[tuple[str, str | None]]) -> str:
    """Render (title, summary) pairs; a falsy summary means the source was unavailable."""
    items = []
    for title, summary in notes:
        if summary:
            items.append(
                f"""    <li class="source-note">
      <span class="source-note__title">{escape(title)}</span>
      <span class="source-note__body">{escape(" ".join(summary.split()))}</span>
    </li>"""
            )
        else:
            items.append(
                f"""    <li class="source-note source-note--failed">
      <span class="source-note__title">{escape(title)}</span>
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


def render_source_notes(reports: list[SourceReport]) -> str:
    # Charts get their own cards on the live page, so only text sources and
    # failures are listed here.
    return _source_notes_section([
        (r.title, r.summary if r.status == "ok" else None)
        for r in reports
        if r.status != "ok" or (r.kind == "text" and r.summary)
    ])


def render_analysis_datetime(generated_at: datetime) -> str:
    iso, human = format_timestamp(generated_at)
    return (
        f'<p class="analysis-datetime">'
        f"<small>Analysis generated: <time datetime=\"{iso}\">{human}</time></small>"
        f"</p>"
    )


def render_content(
    synthesis: Synthesis, reports: list[SourceReport], generated_at: datetime
) -> str:
    sections = [
        render_analysis_datetime(generated_at),
        render_synthesis_section(synthesis, generated_at),
    ]
    for report in reports:
        if report.kind == "image" and report.status == "ok":
            sections.append(render_image_section(report))
    notes = render_source_notes(reports)
    if notes:
        sections.append(notes)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Archive pages (standalone documents derived from per-day history records)
# ---------------------------------------------------------------------------

ARCHIVE_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta
      name="description"
      content="Tlaloc — archived daily synoptic pattern analysis for North America."
    />
    <title>{title}</title>
    <link rel="stylesheet" href="../css/style.css" />
  </head>
  <body>
    <header class="site-header">
      <div class="container">
        <h1 class="site-title">
          <span class="site-title__icon" aria-hidden="true">🌧️</span>
          Tlaloc
        </h1>
        <p class="site-tagline">Daily synoptic pattern analysis for North America</p>
        <nav class="site-nav" aria-label="Site">
          <a href="../">Latest analysis</a>
          <a href="./">Archive</a>
          <a href="../glossary.html">Glossary</a>
        </nav>
      </div>
    </header>

    <main class="main-content">
      <div class="container">
{content}
      </div>
    </main>

    <footer class="site-footer">
      <div class="container">
        <p>
          Named for
          <a
            href="https://en.wikipedia.org/wiki/Tlaloc"
            target="_blank"
            rel="noopener noreferrer"
            >Tláloc</a
          >, the Mexica deity of water, rain, fertility, and storms.
        </p>
        <p>
          Data: NOAA WPC, SPC, NHC, CPC, NESDIS/GOES, Environment and Climate
          Change Canada, Australian Bureau of Meteorology, and College of DuPage NEXLAB. Interpretation and
          synthesis generated with Claude.
        </p>
      </div>
    </footer>
  </body>
</html>
"""


def parse_record_timestamp(record: dict) -> datetime:
    return datetime.fromisoformat(record["generated_at"].replace("Z", "+00:00"))


def render_record_synthesis_section(record: dict) -> str:
    return _synthesis_card(record["synthesis"], "Synoptic Picture", parse_record_timestamp(record))


def render_record_sources_section(record: dict) -> str:
    return _source_notes_section([
        (src["title"], src.get("summary") if src["status"] == "ok" else None)
        for src in record["sources"]
    ])


def render_record_charts_section(record: dict) -> str:
    links = [
        f"""    <li class="chart-link">
      <a href="{escape(source["display_url"], quote=True)}" rel="noopener noreferrer">{escape(source["title"])}</a>
      <span class="chart-link__credit">{escape(source["credit"])}</span>
    </li>"""
        for source in record["sources"]
        if source["kind"] == "image" and source["status"] == "ok" and source.get("display_url")
    ]
    if not links:
        return ""
    body = "\n".join(links)
    return f"""<section aria-labelledby="charts-heading">
  <h2 id="charts-heading">Charts Consulted</h2>
  <p class="archive-note">Chart images are not archived; these links point to the live
  sources, which overwrite or expire their imagery within days.</p>
  <ul class="source-notes">
{body}
  </ul>
</section>"""


def render_archive_page(record: dict) -> str:
    sections = [
        render_analysis_datetime(parse_record_timestamp(record)),
        render_record_synthesis_section(record),
        render_record_sources_section(record),
        render_record_charts_section(record),
    ]
    content = "\n\n".join(s for s in sections if s)
    return ARCHIVE_PAGE_TEMPLATE.format(
        title=f"Tlaloc — Synoptic Analysis for {record['date']}",
        content=content,
    )


def write_archive_page(archive_dir: Path, record: dict) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{record['date']}.html"
    path.write_text(render_archive_page(record))
    return path


def render_archive_index(records: list[dict]) -> str:
    items = []
    for record in records:
        date_str = record["date"]
        headline = record["synthesis"].get("headline", "")
        items.append(
            f"""    <li class="archive-entry">
      <a href="{escape(date_str, quote=True)}.html"><time datetime="{escape(date_str)}">{escape(date_str)}</time></a>
      <span class="archive-entry__headline">{escape(headline)}</span>
    </li>"""
        )
    body = "\n".join(items) if items else "    <li>No archived analyses yet.</li>"
    content = f"""<section aria-labelledby="archive-heading">
  <h2 id="archive-heading">Analysis Archive</h2>
  <ul class="archive-list">
{body}
  </ul>
</section>"""
    return ARCHIVE_PAGE_TEMPLATE.format(title="Tlaloc — Analysis Archive", content=content)


def write_archive_index(archive_dir: Path, records: list[dict]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / "index.html"
    path.write_text(render_archive_index(records))
    return path


def write_index_html(
    index_path: Path,
    synthesis: Synthesis,
    reports: list[SourceReport],
    generated_at: datetime | None = None,
    output_path: Path | None = None,
) -> None:
    """Splice the content into index_path's template; write to output_path if given."""
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
    (output_path or index_path).write_text(updated)
