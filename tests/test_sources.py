"""Tests for the pure logic in tlaloc.sources (no network access)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from tlaloc.fetching import SourceError
from tlaloc.sources import (
    MCD_MAX_PRODUCTS,
    PRE_BLOCK_RE,
    extract_page_image_urls,
    filter_recent_mcd_entries,
    iter_airmass_candidate_urls,
    parse_daily_index_series,
    rank_chart_candidates,
    resolve_500mb_chart_url,
    resolve_first_available_image,
    summarize_daily_index,
)


class TestResolve500mbChartUrl:
    NOW = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)

    def test_returns_most_recent_synoptic_time_when_available(self):
        url = resolve_500mb_chart_url(self.NOW, probe=lambda url: True)
        assert url.endswith("US500.20260715.12.gif")

    def test_steps_back_through_12h_synoptic_times(self):
        probed: list[str] = []

        def probe(url: str) -> bool:
            probed.append(url)
            return len(probed) == 3  # only the third candidate exists

        url = resolve_500mb_chart_url(self.NOW, probe=probe)
        assert url.endswith("US500.20260714.12.gif")
        assert probed[0].endswith("US500.20260715.12.gif")
        assert probed[1].endswith("US500.20260715.00.gif")

    def test_morning_run_starts_at_00z(self):
        morning = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
        url = resolve_500mb_chart_url(morning, probe=lambda url: True)
        assert url.endswith("US500.20260715.00.gif")

    def test_raises_source_error_when_nothing_found(self):
        with pytest.raises(SourceError):
            resolve_500mb_chart_url(self.NOW, probe=lambda url: False)


class TestIterAirmassCandidateUrls:
    NOW = datetime(2026, 7, 15, 14, 37, 42, tzinfo=timezone.utc)

    def test_latest_jpg_offered_first_per_satellite(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW)]
        assert urls[0].endswith("GOES19/ABI/CONUS/AirMass/latest.jpg")
        assert any(url.endswith("GOES16/ABI/CONUS/AirMass/latest.jpg") for url in urls)

    def test_timestamped_candidates_use_julian_day_stamp(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW)]
        # July 15 is day 196 of 2026; the first stamped candidate is the
        # 14:30 scan published at :31.
        assert "20261961431_GOES19-ABI-CONUS-AirMass-2500x1500.jpg" in urls[1]

    def test_no_candidate_is_in_the_future(self):
        for url, _sat in iter_airmass_candidate_urls(self.NOW):
            if url.endswith("latest.jpg"):
                continue
            stamp = url.rsplit("/", 1)[1].split("_", 1)[0]
            assert datetime.strptime(stamp, "%Y%j%H%M").replace(tzinfo=timezone.utc) <= self.NOW

    def test_satellites_ordered_newest_first(self):
        sats = [sat for _url, sat in iter_airmass_candidate_urls(self.NOW)]
        assert sats[0] == "GOES19"
        assert sats.index("GOES16") == sats.count("GOES19")

    def test_full_disk_sector_uses_fd_path_and_size(self):
        urls = [url for url, _sat in iter_airmass_candidate_urls(self.NOW, sector="FD")]
        assert urls[0].endswith("GOES19/ABI/FD/AirMass/latest.jpg")
        assert "20261961431_GOES19-ABI-FD-AirMass-1808x1808.jpg" in urls[1]
        assert not any("CONUS" in url for url in urls)


class TestFilterRecentMcdEntries:
    NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)

    @staticmethod
    def entry(issued: str, id_suffix: str = "x") -> dict:
        return {"issuanceTime": issued, "@id": f"https://api.weather.gov/products/{id_suffix}"}

    def test_keeps_only_entries_within_lookback(self):
        entries = [
            self.entry("2026-07-15T17:30:00+00:00", "fresh"),
            self.entry("2026-07-15T11:00:00+00:00", "stale"),
        ]
        recent = filter_recent_mcd_entries(entries, self.NOW)
        assert [e["@id"].rsplit("/", 1)[1] for e in recent] == ["fresh"]

    def test_caps_at_max_products(self):
        entries = [self.entry("2026-07-15T17:00:00Z", str(i)) for i in range(10)]
        assert len(filter_recent_mcd_entries(entries, self.NOW)) == MCD_MAX_PRODUCTS

    def test_skips_malformed_entries(self):
        entries = [
            {"issuanceTime": "not-a-date", "@id": "u"},
            {"@id": "missing-time"},
            {"issuanceTime": "2026-07-15T17:00:00Z"},  # missing @id
        ]
        assert filter_recent_mcd_entries(entries, self.NOW) == []


class TestResolveFirstAvailableImage:
    CANDIDATES = ("https://example.test/a.gif", "https://example.test/b.gif")

    def test_prefers_the_first_candidate_that_exists(self):
        url = resolve_first_available_image(self.CANDIDATES, probe=lambda url: True)
        assert url == self.CANDIDATES[0]

    def test_falls_back_to_a_later_naming_convention(self):
        url = resolve_first_available_image(
            self.CANDIDATES, probe=lambda url: url.endswith("b.gif")
        )
        assert url == self.CANDIDATES[1]

    def test_raises_source_error_when_no_candidate_exists(self):
        with pytest.raises(SourceError):
            resolve_first_available_image(self.CANDIDATES, probe=lambda url: False)


class TestPageImageDiscovery:
    PAGE = "https://www.cpc.ncep.noaa.gov/products/predictions/610day/500mb.php"
    DIR = "https://www.cpc.ncep.noaa.gov/products/predictions/610day/"

    def ranked(self, html: str) -> list[str]:
        return rank_chart_candidates(self.PAGE, extract_page_image_urls(self.PAGE, html))

    def test_resolves_relative_srcs_against_the_page(self):
        html = '<img src="../../images/noaa_logo.gif"><img src="500mbfcst.gif">'
        assert extract_page_image_urls(self.PAGE, html) == [
            "https://www.cpc.ncep.noaa.gov/products/images/noaa_logo.gif",
            f"{self.DIR}500mbfcst.gif",
        ]

    def test_deduplicates_repeated_references(self):
        html = '<img src="hgt.gif"><img class="x" src="hgt.gif">'
        assert extract_page_image_urls(self.PAGE, html) == [f"{self.DIR}hgt.gif"]

    def test_rejects_images_outside_the_pages_own_directory(self):
        # The accessibility spacer that shipped a blank chart to the vision model.
        html = '<img src="/nwscwi/skipgraphic.gif"><img src="500mb.gif">'
        assert self.ranked(html) == [f"{self.DIR}500mb.gif"]

    def test_drops_site_chrome_and_non_images(self):
        html = """
        <img src="banner.gif"><img src="btn_next.png"><img src="arrow.gif">
        <img src="/products/predictions/610day/hgt.gif">
        <a href="somewhere.php">text</a>
        """
        assert self.ranked(html) == [f"{self.DIR}hgt.gif"]

    def test_orders_product_looking_filenames_first(self):
        html = '<img src="thumbnail.png"><img src="610_500mb_anom.gif">'
        assert self.ranked(html) == [f"{self.DIR}610_500mb_anom.gif", f"{self.DIR}thumbnail.png"]

    def test_returns_empty_when_the_page_has_no_chart(self):
        assert self.ranked("<html><body>nothing</body></html>") == []


class TestDailyIndexSeries:
    TABLE = "\n".join(
        [
            "1950 1 1 0.92",
            "2026 7 16 1.40",
            "2026 7 17 1.30",
            "2026 7 18 1.20",
            "2026 7 19 1.10",
            "2026 7 20 1.00",
            "2026 7 21 0.90",
            "2026 7 22 0.80",
            "2026 7 23 0.70",
            "2026 7 24 0.60",
            "2026 7 25 0.50",
            "2026 7 26 0.40",
            "2026 7 27 0.30",
            "2026 7 28 0.20",
            "2026 7 29 0.10",
        ]
    )

    def test_parses_date_and_value_pairs(self):
        series = parse_daily_index_series(self.TABLE)
        assert series[0] == (date(1950, 1, 1), 0.92)
        assert series[-1] == (date(2026, 7, 29), 0.10)

    def test_skips_headers_and_missing_data_sentinels(self):
        text = "PNA index\n2026 7 28 -999.0\n2026 7 29 0.10\nnot a row\n"
        assert parse_daily_index_series(text) == [(date(2026, 7, 29), 0.10)]

    def test_summary_reports_latest_value_and_falling_trend(self):
        summary = summarize_daily_index("PNA", "note", parse_daily_index_series(self.TABLE))
        assert "latest 2026-07-29: +0.10" in summary
        # Last week averages 0.7 below the prior week: a real relaxation.
        assert "-0.70, falling" in summary
        assert summary.endswith(
            "daily, oldest to newest: +1.40 +1.30 +1.20 +1.10 +1.00 +0.90 +0.80 "
            "+0.70 +0.60 +0.50 +0.40 +0.30 +0.20 +0.10"
        )

    def test_summary_names_no_trend_for_a_steady_index(self):
        steady = "\n".join(f"2026 7 {day} 0.50" for day in range(16, 30))
        summary = summarize_daily_index("PNA", "note", parse_daily_index_series(steady))
        assert "little changed" in summary

    def test_summary_handles_a_series_shorter_than_two_weeks(self):
        short = "2026 7 28 0.40\n2026 7 29 0.60"
        summary = summarize_daily_index("AO", "note", parse_daily_index_series(short))
        assert "no prior week available" in summary

    def test_summary_raises_when_nothing_parsed(self):
        with pytest.raises(SourceError):
            summarize_daily_index("PNA", "note", [])


class TestPreBlockRegex:
    def test_extracts_product_text_from_nhc_page(self):
        html = "<html><body><PRE>\nABNT20 KNHC\nTropical outlook body\n</PRE></body></html>"
        match = PRE_BLOCK_RE.search(html)
        assert match is not None
        assert "Tropical outlook body" in match.group(1)

    def test_no_match_without_pre_block(self):
        assert PRE_BLOCK_RE.search("<html><body>nothing here</body></html>") is None


class TestTeleconnectionFreshness:
    @staticmethod
    def table(last_day: date, value: float = 0.5) -> str:
        rows = []
        for i in range(20, -1, -1):
            day = last_day - timedelta(days=i)
            rows.append(f"{day.year} {day.month} {day.day} {value}")
        return "\n".join(rows)

    def run_collector(self, monkeypatch, tables_by_host):
        from tlaloc import sources

        def fake_fetch(url, max_chars):
            for host, text in tables_by_host.items():
                if url.startswith(host):
                    if text is None:
                        raise sources.SourceError("down")
                    return text
            raise sources.SourceError("unknown host")

        monkeypatch.setattr(sources, "fetch_text", fake_fetch)
        return sources.collect_teleconnection_indices()

    def test_prefers_the_fresher_host_and_skips_warning(self, monkeypatch):
        from tlaloc import sources

        today = datetime.now(timezone.utc).date()
        stale_host, fresh_host = sources.CPC_DAILY_INDEX_HOSTS
        report = self.run_collector(monkeypatch, {
            stale_host: self.table(today - timedelta(days=9), 0.1),
            fresh_host: self.table(today - timedelta(days=1), 0.9),
        })
        assert report.status == "ok"
        assert "+0.90" in report.raw_text
        assert "STALE" not in report.raw_text

    def test_flags_stale_data_when_every_host_is_old(self, monkeypatch):
        from tlaloc import sources

        today = datetime.now(timezone.utc).date()
        hosts = sources.CPC_DAILY_INDEX_HOSTS
        report = self.run_collector(
            monkeypatch, {h: self.table(today - timedelta(days=8)) for h in hosts}
        )
        assert report.status == "ok"
        assert "STALE" in report.raw_text
        assert "8 days old" in report.raw_text


class TestActiveStorms:
    STORM = {
        "id": "al092026",
        "binNumber": "AT4",
        "name": "Isaias",
        "classification": "HU",
        "intensity": "75",
        "pressure": "975",
        "latitude": "25.1N",
        "longitude": "86.2W",
        "movementDir": 350,
        "movementSpeed": 8,
        "lastUpdate": "2026-10-08T21:00:00.000Z",
        "publicAdvisory": {"url": "https://www.nhc.noaa.gov/text/MIATCPAT4.shtml"},
    }

    def test_describe_storm_uses_available_fields(self):
        from tlaloc.sources import describe_active_storm

        line = describe_active_storm(self.STORM)
        assert line.startswith("Hurricane Isaias")
        assert "75 kt" in line and "975 mb" in line and "25.1N, 86.2W" in line

    def test_describe_storm_tolerates_missing_fields(self):
        from tlaloc.sources import describe_active_storm

        assert describe_active_storm({"name": "Simon", "classification": "TS"}) == (
            "Tropical Storm Simon"
        )

    def test_product_url_prefers_nhc_link_then_builds_from_bin(self):
        from tlaloc.sources import _nhc_product_url

        assert _nhc_product_url(self.STORM, "publicAdvisory", "TCP").endswith("MIATCPAT4.shtml")
        assert _nhc_product_url(self.STORM, "forecastDiscussion", "TCD") == (
            "https://www.nhc.noaa.gov/text/MIATCDAT4.shtml"
        )
        assert _nhc_product_url({}, "forecastDiscussion", "TCD") is None

    def test_no_active_storms_is_a_note_not_a_failure(self, monkeypatch):
        from tlaloc import sources

        monkeypatch.setattr(sources, "fetch_json", lambda url: {"activeStorms": []})
        report = sources.collect_active_storms()
        assert report.status == "ok"
        assert report.raw_text == sources.NO_ACTIVE_STORMS_TEXT

    def test_unreadable_advisory_still_reports_the_storm(self, monkeypatch):
        from tlaloc import sources

        def no_text(url, max_chars):
            raise sources.SourceError("down")

        monkeypatch.setattr(sources, "fetch_json", lambda url: {"activeStorms": [self.STORM]})
        monkeypatch.setattr(sources, "fetch_text", no_text)
        report = sources.collect_active_storms()
        assert report.status == "ok"
        assert "Hurricane Isaias" in report.raw_text
        assert "Public advisory unavailable" in report.raw_text

    def test_index_failure_fails_the_source(self, monkeypatch):
        from tlaloc import sources

        def boom(url):
            raise sources.SourceError("403")

        monkeypatch.setattr(sources, "fetch_json", boom)
        assert sources.collect_active_storms().status == "failed"


class TestMjo:
    TABLE = "\n".join([
        "RMM index, rows: year month day RMM1 RMM2 phase amplitude status",
        "1974 6 1 1.2 -0.4 5 1.26 Final",
        "1974 6 2 1.E36 1.E36 999 1.E36 missing",
    ] + [
        f"2026 10 {d} {0.2 * d:.2f} -0.50 {1 + d % 8} {0.3 * d:.2f} Prelim" for d in range(1, 9)
    ])

    def test_parse_skips_header_and_missing_rows(self):
        from tlaloc.sources import parse_rmm_series

        series = parse_rmm_series(self.TABLE)
        assert len(series) == 9
        assert series[0][0] == date(1974, 6, 1) and series[0][3] == 5
        assert series[-1][0] == date(2026, 10, 8)

    def test_summary_reports_phase_amplitude_and_staleness(self):
        from tlaloc.sources import parse_rmm_series, summarize_mjo

        series = parse_rmm_series(self.TABLE)
        fresh = summarize_mjo(series, date(2026, 10, 9))
        assert "Latest 2026-10-08: phase 1 (Western Hemisphere/Africa)" in fresh
        assert "active (outside the unit circle)" in fresh
        assert "STALE" not in fresh
        stale = summarize_mjo(series, date(2026, 10, 20))
        assert "STALE" in stale and "12 days old" in stale

    def test_weak_amplitude_is_labeled_weak(self):
        from tlaloc.sources import summarize_mjo

        text = summarize_mjo([(date(2026, 10, 8), 0.1, 0.1, 3, 0.4)], date(2026, 10, 8))
        assert "weak (inside the unit circle" in text


class TestEroCollectors:
    def test_failure_names_what_the_page_offers(self, monkeypatch):
        from tlaloc import sources

        monkeypatch.setattr(sources, "image_url_exists", lambda url: False)
        monkeypatch.setattr(
            sources,
            "fetch_text",
            lambda url, max_chars: '<img src="/qpf/new_ero_day1.png"><img src="/logo.png">',
        )
        report = sources.collect_ero_day1()
        assert report.status == "failed"
        assert "new_ero_day1.png" in report.error
