"""The core source registry and collectors.

Design note: the core sources are deliberately hardcoded. A daily synoptic
overview needs the same backbone every day — surface analysis, 500 mb
heights, water-vapor/airmass imagery, and the major center discussions — so
no model is asked to decide *whether* to fetch them. Dynamic, on-demand
source selection happens later, in the synthesis stage, where Claude gets a
small menu of supplementary products it can pull if the core data raises
questions (see synthesize.py).

Each collector returns a SourceReport. Collectors never raise: any failure is
captured on the report so the pipeline can degrade gracefully.
"""

import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from .config import MAX_TEXT_CHARS
from .fetching import SourceError, fetch_image_base64, fetch_json, fetch_text, image_url_exists

Kind = Literal["image", "text"]


@dataclass
class SourceReport:
    key: str
    title: str
    kind: Kind
    credit: str
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    # Image sources
    display_url: str | None = None
    image_base64: str | None = None
    image_media_type: str | None = None
    # Text sources
    raw_text: str | None = None
    # Filled in by the interpretation stage
    summary: str | None = None

    def fail(self, error: str) -> "SourceReport":
        self.status = "failed"
        self.error = error
        return self


# ---------------------------------------------------------------------------
# Image sources
# ---------------------------------------------------------------------------

WPC_SURFACE_URL = "https://www.wpc.ncep.noaa.gov/sfc/namussfcwbg.gif"

COD_500MB_URL_TEMPLATE = "https://weather.cod.edu/wxdata/upper/US/500/US500.{date}.{hour}.gif"
COD_500MB_LOOKBACK_STEPS = 4  # 12-hour synoptic times to look back

GOES_AIRMASS_BASE = "https://cdn.star.nesdis.noaa.gov/{satellite}/ABI/CONUS/AirMass"
GOES_AIRMASS_FILENAME = "{stamp}_{satellite}-ABI-CONUS-AirMass-2500x1500.jpg"
GOES_SATELLITES = ("GOES19", "GOES16")
GOES_LOOKBACK_HOURS = 3


def collect_surface_analysis() -> SourceReport:
    report = SourceReport(
        key="surface",
        title="Surface Analysis",
        kind="image",
        credit="NOAA Weather Prediction Center",
    )
    try:
        data, media_type = fetch_image_base64(WPC_SURFACE_URL)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = WPC_SURFACE_URL
    report.image_base64 = data
    report.image_media_type = media_type
    return report


def resolve_500mb_chart_url(now_utc: datetime | None = None) -> str:
    """Find the most recent COD 500 mb analysis, stepping back 12 h at a time."""
    probe_time = now_utc or datetime.now(timezone.utc)
    hour = 12 if probe_time.hour >= 12 else 0
    aligned = probe_time.replace(hour=hour, minute=0, second=0, microsecond=0)
    for step in range(COD_500MB_LOOKBACK_STEPS):
        t = aligned - timedelta(hours=12 * step)
        url = COD_500MB_URL_TEMPLATE.format(date=t.strftime("%Y%m%d"), hour=t.strftime("%H"))
        if image_url_exists(url):
            return url
    raise SourceError(
        f"No COD 500mb chart found within {COD_500MB_LOOKBACK_STEPS} synoptic times of "
        f"{probe_time:%Y-%m-%dT%H:00Z}"
    )


def collect_500mb_analysis() -> SourceReport:
    report = SourceReport(
        key="upper_500mb",
        title="500 mb Analysis",
        kind="image",
        credit="College of DuPage NEXLAB",
    )
    try:
        url = resolve_500mb_chart_url()
        data, media_type = fetch_image_base64(url)
    except SourceError as exc:
        return report.fail(str(exc))
    report.display_url = url
    report.image_base64 = data
    report.image_media_type = media_type
    return report


def iter_airmass_candidate_urls(now_utc: datetime):
    """Yield candidate GOES Air Mass URLs, newest-first per satellite.

    Tries the stable latest.jpg first, then timestamped 10-minute scans
    (stamped :01 then :00, matching how recent products are published).
    """
    normalized = now_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    aligned = normalized - timedelta(minutes=normalized.minute % 10)
    for satellite in GOES_SATELLITES:
        base = GOES_AIRMASS_BASE.format(satellite=satellite)
        yield f"{base}/latest.jpg", satellite
        for step in range(GOES_LOOKBACK_HOURS * 6):
            scan = aligned - timedelta(minutes=10 * step)
            for minute_offset in (1, 0):
                candidate = scan + timedelta(minutes=minute_offset)
                if candidate > normalized:
                    continue
                stamp = candidate.strftime("%Y%j%H%M")
                filename = GOES_AIRMASS_FILENAME.format(stamp=stamp, satellite=satellite)
                yield f"{base}/{filename}", satellite


def collect_airmass_rgb() -> SourceReport:
    report = SourceReport(
        key="airmass",
        title="Air Mass RGB",
        kind="image",
        credit="NOAA NESDIS / GOES East",
    )
    now_utc = datetime.now(timezone.utc)
    for url, satellite in iter_airmass_candidate_urls(now_utc):
        if not image_url_exists(url):
            continue
        try:
            data, media_type = fetch_image_base64(url)
        except SourceError:
            continue  # e.g. latest.jpg over the 5 MB limit; try the next candidate
        report.title = f"Air Mass RGB ({satellite.replace('GOES', 'GOES-')})"
        report.display_url = url
        report.image_base64 = data
        report.image_media_type = media_type
        return report
    return report.fail(
        f"No usable GOES CONUS Air Mass image found within {GOES_LOOKBACK_HOURS}h lookback"
    )


# ---------------------------------------------------------------------------
# Text sources
# ---------------------------------------------------------------------------

NWS_PRODUCT_LATEST = "https://api.weather.gov/products/types/{type_id}/locations/{location}/latest"
NWS_PRODUCT_LIST = "https://api.weather.gov/products/types/{type_id}/locations/{location}"


def fetch_nws_product_text(type_id: str, location: str) -> str:
    """Fetch the latest text product of a given type from api.weather.gov.

    The API splits the six-character AWIPS PIL into a 3-letter product type
    (NNN) and a 3-letter location (XXX): SWODY1 -> type SWO, location DY1,
    just like AFD/MPX. Tries the /latest endpoint first; if its response
    shape is unexpected, falls back to listing products and fetching the
    newest one.
    """
    latest_url = NWS_PRODUCT_LATEST.format(type_id=type_id, location=location)
    try:
        data = fetch_json(latest_url)
        text = data.get("productText")
        if isinstance(text, str) and text.strip():
            return text.strip()[:MAX_TEXT_CHARS]
    except SourceError:
        pass  # fall through to the list endpoint

    listing = fetch_json(NWS_PRODUCT_LIST.format(type_id=type_id, location=location))
    entries = listing.get("@graph") or []
    if not entries or not isinstance(entries[0], dict) or "@id" not in entries[0]:
        raise SourceError(f"No {type_id}/{location} products listed by api.weather.gov")
    product = fetch_json(entries[0]["@id"])
    text = product.get("productText")
    if not isinstance(text, str) or not text.strip():
        raise SourceError(f"Latest {type_id}/{location} product has no text body")
    return text.strip()[:MAX_TEXT_CHARS]


def collect_wpc_discussion() -> SourceReport:
    report = SourceReport(
        key="wpc_discussion",
        title="WPC Short Range Forecast Discussion",
        kind="text",
        credit="NOAA Weather Prediction Center",
    )
    try:
        report.raw_text = fetch_nws_product_text("PMD", "SPD")
    except SourceError as exc:
        return report.fail(str(exc))
    return report


def collect_spc_outlook() -> SourceReport:
    report = SourceReport(
        key="spc_outlook",
        title="SPC Day 1 Convective Outlook",
        kind="text",
        credit="NOAA Storm Prediction Center",
    )
    try:
        report.raw_text = fetch_nws_product_text("SWO", "DY1")
    except SourceError as exc:
        return report.fail(str(exc))
    return report


NHC_TWO_PAGES = {
    "Atlantic": "https://www.nhc.noaa.gov/text/MIATWOAT.shtml",
    "East Pacific": "https://www.nhc.noaa.gov/text/MIATWOEP.shtml",
}
PRE_BLOCK_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)


def collect_tropical_outlooks() -> SourceReport:
    report = SourceReport(
        key="tropics",
        title="NHC Tropical Weather Outlooks",
        kind="text",
        credit="NOAA National Hurricane Center",
    )
    sections = []
    errors = []
    for basin, url in NHC_TWO_PAGES.items():
        try:
            html = fetch_text(url, MAX_TEXT_CHARS)
        except SourceError as exc:
            errors.append(f"{basin}: {exc}")
            continue
        match = PRE_BLOCK_RE.search(html)
        if match:
            sections.append(f"--- {basin} ---\n{match.group(1).strip()}")
        else:
            errors.append(f"{basin}: no <pre> product block found at {url}")
    if not sections:
        return report.fail("; ".join(errors) or "no outlooks retrieved")
    report.raw_text = "\n\n".join(sections)[:MAX_TEXT_CHARS]
    return report


ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"


def collect_enso_state() -> SourceReport:
    report = SourceReport(
        key="enso",
        title="ENSO State (Oceanic Niño Index)",
        kind="text",
        credit="NOAA Climate Prediction Center",
    )
    try:
        # The ONI table is chronological from 1950 and larger than the default
        # text cap; fetch it whole so the tail is genuinely the most recent data.
        oni = fetch_text(ONI_URL, max_chars=200_000)
    except SourceError as exc:
        return report.fail(str(exc))
    lines = oni.splitlines()
    if len(lines) < 9:
        return report.fail(f"ONI table at {ONI_URL} has unexpected shape")
    # Header plus the most recent ~8 overlapping seasons is plenty of context.
    report.raw_text = "\n".join([lines[0]] + lines[-8:])
    return report


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COLLECTORS: list[Callable[[], SourceReport]] = [
    collect_surface_analysis,
    collect_500mb_analysis,
    collect_airmass_rgb,
    collect_wpc_discussion,
    collect_spc_outlook,
    collect_tropical_outlooks,
    collect_enso_state,
]


def collect_all() -> list[SourceReport]:
    """Run every collector, converting unexpected exceptions to failed reports."""
    reports = []
    for collector in COLLECTORS:
        try:
            report = collector()
        except Exception:  # a collector bug must not kill the run
            name = collector.__name__.removeprefix("collect_")
            report = SourceReport(
                key=name, title=name, kind="text", credit="unknown"
            ).fail(f"Collector crashed:\n{traceback.format_exc(limit=3)}")
        reports.append(report)
    return reports
