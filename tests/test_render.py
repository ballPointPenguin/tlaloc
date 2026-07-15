"""Tests for HTML rendering, especially the sentinel-replacement round trip."""

from datetime import datetime, timezone

import pytest

from tlaloc.render import paragraphs, write_index_html
from tlaloc.sources import SourceReport
from tlaloc.synthesize import Synthesis

SENTINEL_PAGE = """<html>
  <body>
    <main>
        <!-- BEGIN TLALOC CONTENT -->
        stale content from yesterday
        <!-- END TLALOC CONTENT -->
    </main>
  </body>
</html>
"""

GENERATED_AT = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def make_synthesis(**overrides) -> Synthesis:
    fields = {
        "headline": "A quiet ridge dominates",
        "narrative": "First paragraph.\n\nSecond paragraph.",
        "climate_context": "ENSO-neutral conditions persist.",
    }
    fields.update(overrides)
    return Synthesis(**fields)


def make_image_report() -> SourceReport:
    report = SourceReport(
        key="surface",
        title="Surface Analysis",
        kind="image",
        credit="NOAA Weather Prediction Center",
    )
    report.display_url = "https://example.test/surface.gif"
    report.summary = "High pressure centered over the Plains."
    return report


class TestParagraphs:
    def test_splits_on_blank_lines(self):
        html = paragraphs("one\n\ntwo\n\n\nthree", "cls")
        assert html.count('<p class="cls">') == 3

    def test_collapses_internal_whitespace(self):
        html = paragraphs("spread   over\nlines", "cls")
        assert "spread over lines" in html

    def test_escapes_html(self):
        html = paragraphs("a < b & c", "cls")
        assert "a &lt; b &amp; c" in html


class TestWriteIndexHtml:
    def test_replaces_content_between_sentinels(self, tmp_path):
        index = tmp_path / "index.html"
        index.write_text(SENTINEL_PAGE)
        write_index_html(index, make_synthesis(), [make_image_report()], GENERATED_AT)
        updated = index.read_text()
        assert "stale content from yesterday" not in updated
        assert "A quiet ridge dominates" in updated
        assert "<!-- BEGIN TLALOC CONTENT -->" in updated
        assert "<!-- END TLALOC CONTENT -->" in updated

    def test_backslashes_in_synthesis_are_preserved(self, tmp_path):
        # Regression: re.sub with a string replacement would mangle
        # backslash sequences like \g<1>; render uses a callable instead.
        index = tmp_path / "index.html"
        index.write_text(SENTINEL_PAGE)
        synthesis = make_synthesis(narrative=r"Winds back \ then veer \g<1> overnight.")
        write_index_html(index, synthesis, [], GENERATED_AT)
        assert r"\g&lt;1&gt;" in index.read_text()

    def test_missing_sentinels_raise(self, tmp_path):
        index = tmp_path / "index.html"
        index.write_text("<html><body>no markers</body></html>")
        with pytest.raises(RuntimeError, match="sentinel"):
            write_index_html(index, make_synthesis(), [], GENERATED_AT)

    def test_regional_notes_section_only_when_present(self, tmp_path):
        index = tmp_path / "index.html"
        index.write_text(SENTINEL_PAGE)
        write_index_html(index, make_synthesis(), [], GENERATED_AT)
        assert "Regional Signals" not in index.read_text()
        write_index_html(
            index,
            make_synthesis(regional_notes="MCD 1701 tracks supercells in Alberta."),
            [],
            GENERATED_AT,
        )
        html = index.read_text()
        assert "Regional Signals" in html
        assert "MCD 1701 tracks supercells in Alberta." in html

    def test_rewrite_is_idempotent(self, tmp_path):
        index = tmp_path / "index.html"
        index.write_text(SENTINEL_PAGE)
        write_index_html(index, make_synthesis(), [make_image_report()], GENERATED_AT)
        first = index.read_text()
        write_index_html(index, make_synthesis(), [make_image_report()], GENERATED_AT)
        assert index.read_text() == first
