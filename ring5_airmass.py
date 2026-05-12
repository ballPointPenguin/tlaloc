import json
import re
import base64
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
GOES_AIRMASS_BASE_URL_TEMPLATE = "https://cdn.star.nesdis.noaa.gov/{satellite}/ABI/CONUS/AirMass"
GOES_AIRMASS_FILENAME_TEMPLATE = "{stamp}_{satellite}-ABI-CONUS-AirMass-2500x1500.jpg"
GOES_AIRMASS_SATELLITES = ("GOES19", "GOES16")
GOES_AIRMASS_LABELS = {"GOES19": "GOES-19", "GOES16": "GOES-16"}
MAX_ANTHROPIC_IMAGE_BYTES = 5 * 1024 * 1024
URL_PROBE_TIMEOUT_SECONDS = 10
AIRMASS_LOOKBACK_HOURS = 6
AIRMASS_LOOKBACK_STEPS = AIRMASS_LOOKBACK_HOURS * 6
AIRMASS_MINUTE_PROBE_OFFSETS = (1, 0)

AIRMASS_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN AIRMASS CONTENT -->).*?([ \t]*<!-- END AIRMASS CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided GOES East CONUS Air Mass RGB satellite image.
   Color heuristics: reds/oranges indicate dry stratospheric intrusions or high-PV air;
   greens indicate tropical moist air; dark blues indicate cold dry upper troposphere;
   white indicates high cold cloud tops.
2. Identify the most important visible upper-level features: stratospheric dry air intrusions,
   upper-level moisture patterns, jet stream position and dynamics, thermal structure,
   and any potential vorticity (PV) streamers or cutoffs.
3. Call interpret_airmass_chart with a brief 2-4 sentence interpretation grounded in the image.
4. Call update_airmass_content with the interpretation text and the provided ISO 8601 timestamp.

Keep the interpretation concise, meteorological, and cautious. Mention only features that are
clearly visible in the image. Use plain text, not markdown.
"""

TOOLS = [
    {
        "name": "interpret_airmass_chart",
        "description": (
            "Return your brief interpretation of the provided Air Mass RGB satellite image. "
            "Summarize the most important visible features: stratospheric dry intrusions, "
            "upper-level moisture, jet dynamics, and any PV streamers in 2-4 sentences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text interpretation of the Air Mass RGB image",
                },
                "generated_at": {
                    "type": "string",
                    "description": "The provided ISO 8601 timestamp to show in the page",
                },
            },
            "required": ["interpretation", "generated_at"],
        },
    },
    {
        "name": "update_airmass_content",
        "description": (
            "Replace the Air Mass RGB content block in index.html. "
            "The interpretation is rendered below a live GOES East Air Mass RGB image between "
            "<!-- BEGIN AIRMASS CONTENT --> and <!-- END AIRMASS CONTENT --> markers. "
            "The generated HTML reuses these CSS classes: synoptic-card, synoptic-card__image, "
            "synoptic-card__body, synoptic-card__text, synoptic-card__timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text interpretation to place below the image",
                },
                "generated_at": {
                    "type": "string",
                    "description": "ISO 8601 timestamp of when the image was interpreted",
                },
            },
            "required": ["interpretation", "generated_at"],
        },
    },
]


def format_timestamp(generated_at: str) -> str:
    dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%B')} {dt.day}, {dt.year} at {time_text} UTC"


def interpret_airmass_chart(interpretation: str, generated_at: str) -> dict:
    normalized = " ".join(interpretation.split())
    return {
        "success": True,
        "interpretation": normalized,
        "generated_at": generated_at,
    }


def build_airmass_html(
    interpretation: str, generated_at: str, image_url: str, satellite_label: str
) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="airmass-heading">
  <h2 id="airmass-heading">Air Mass RGB</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{image_url}"
      alt="{satellite_label} CONUS Air Mass RGB satellite image"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_airmass_content(
    interpretation: str, generated_at: str, image_url: str, satellite_label: str
) -> dict:
    source = INDEX_HTML.read_text()
    html = build_airmass_html(interpretation, generated_at, image_url, satellite_label)
    replacement = (
        "        <!-- BEGIN AIRMASS CONTENT -->\n"
        f"{html}\n"
        "        <!-- END AIRMASS CONTENT -->"
    )
    updated, count = AIRMASS_SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "Air Mass RGB sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict, image_url: str, satellite_label: str) -> dict:
    if name == "interpret_airmass_chart":
        return interpret_airmass_chart(**inputs)
    if name == "update_airmass_content":
        return update_airmass_content(
            interpretation=inputs["interpretation"],
            generated_at=inputs["generated_at"],
            image_url=image_url,
            satellite_label=satellite_label,
        )
    raise ValueError(f"Unknown tool: {name}")


def fetch_airmass_image(url: str) -> tuple[str, str, int]:
    request = Request(url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                content_length_int = int(content_length)
            else:
                content_length_int = None

            if content_length_int and content_length_int > MAX_ANTHROPIC_IMAGE_BYTES:
                raise ValueError(
                    f"Air Mass image size ({content_length_int} bytes) exceeds Anthropic's 5MB limit. "
                    "Unable to process this image."
                )

            content_type_header = response.headers.get("Content-Type", "")
            media_type = content_type_header.split(";", 1)[0].strip().lower()
            if not media_type.startswith("image/"):
                raise ValueError(
                    "Unexpected Air Mass content type: "
                    f"parsed={media_type!r}, header={content_type_header!r}"
                )

            image_bytes = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                image_bytes.extend(chunk)
                if len(image_bytes) > MAX_ANTHROPIC_IMAGE_BYTES:
                    raise ValueError(
                        f"Downloaded Air Mass image size ({len(image_bytes)} bytes) exceeds "
                        "Anthropic's 5MB limit. Unable to process this image."
                    )
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"Failed to download Air Mass image from {url}: {exc}") from exc

    if not image_bytes:
        raise ValueError("Air Mass image download returned no data")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return encoded, media_type, len(image_bytes)


def image_url_exists(url: str) -> bool:
    """Return True when URL is reachable and serves image content."""
    request = Request(url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
    try:
        with urlopen(request, timeout=URL_PROBE_TIMEOUT_SECONDS) as response:
            content_type_header = response.headers.get("Content-Type", "")
            media_type = content_type_header.split(";", 1)[0].strip().lower()
            return media_type.startswith("image/")
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"Failed to check Air Mass image URL {url}: {exc}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"Failed to check Air Mass image URL {url}: {exc}") from exc


def iter_airmass_candidate_urls(now_utc: datetime):
    """Yield candidate (url, satellite) pairs for Air Mass image probing.

    Args:
        now_utc: Current UTC time used to align candidate timestamps.

    Yields:
        Tuples of (candidate_url, satellite_code), ordered by preferred recency
        and satellite fallback priority.
    """
    normalized = now_utc.astimezone(timezone.utc).replace(second=0, microsecond=0)
    aligned = normalized - timedelta(minutes=normalized.minute % 10)
    for satellite in GOES_AIRMASS_SATELLITES:
        base_url = GOES_AIRMASS_BASE_URL_TEMPLATE.format(satellite=satellite)
        yield f"{base_url}/latest.jpg", satellite
        for step in range(AIRMASS_LOOKBACK_STEPS):
            scan_time = aligned - timedelta(minutes=10 * step)
            # Probe :01 first, then :00, because recent Air Mass products are commonly stamped :01.
            for minute_adjustment in AIRMASS_MINUTE_PROBE_OFFSETS:
                candidate_scan_time = scan_time + timedelta(minutes=minute_adjustment)
                if candidate_scan_time > normalized:
                    continue
                stamp = candidate_scan_time.strftime("%Y%j%H%M")
                filename = GOES_AIRMASS_FILENAME_TEMPLATE.format(stamp=stamp, satellite=satellite)
                yield f"{base_url}/{filename}", satellite


def resolve_airmass_image_url(now_utc: datetime | None = None) -> tuple[str, str]:
    """Resolve a usable GOES CONUS Air Mass image URL.

    Args:
        now_utc: Optional UTC timestamp used to generate candidate URLs.

    Returns:
        A tuple of (image_url, satellite_label), preferring GOES-19 and
        falling back to GOES-16 when needed.
    """
    probe_time = now_utc or datetime.now(timezone.utc)
    for url, satellite in iter_airmass_candidate_urls(probe_time):
        if image_url_exists(url):
            return url, GOES_AIRMASS_LABELS.get(satellite, satellite)
    raise RuntimeError("Failed to locate a recent GOES CONUS Air Mass image URL")


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    try:
        airmass_image_url, airmass_satellite_label = resolve_airmass_image_url()
    except RuntimeError as exc:
        raise RuntimeError(
            "Unable to resolve a current GOES CONUS Air Mass image URL after trying GOES-19 "
            "and GOES-16 candidates over the configured lookback window."
        ) from exc
    image_data, image_media_type, image_size = fetch_airmass_image(airmass_image_url)
    print(f"Resolved Air Mass image URL: {airmass_image_url}")
    print(f"Fetched Air Mass image bytes: {image_size}")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Interpret this {airmass_satellite_label} CONUS Air Mass RGB satellite "
                        "image and update the "
                        "Tlaloc page with a brief synoptic summary. Focus on stratospheric dry air "
                        "intrusions, upper-level moisture, jet dynamics, thermal structure, and "
                        "any PV streamers. Use this exact ISO 8601 timestamp when calling tools: "
                        f"{generated_at}"
                    ),
                },
            ],
        }
    ]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        print(f"stop_reason: {response.stop_reason}")

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"tool call: {block.name}({json.dumps(block.input, indent=2)})\n")
            result = run_tool(
                block.name,
                block.input,
                image_url=airmass_image_url,
                satellite_label=airmass_satellite_label,
            )
            print(f"tool result: {json.dumps(result, indent=2)}\n")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    for block in response.content:
        if block.type == "text":
            print("---")
            print(block.text)


if __name__ == "__main__":
    main()
