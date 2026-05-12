import json
import re
import base64
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
AIRMASS_RGB_URL = "https://cdn.star.nesdis.noaa.gov/GOES16/ABI/CONUS/AirMass/latest.jpg"
MAX_ANTHROPIC_IMAGE_BYTES = 5 * 1024 * 1024

AIRMASS_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN AIRMASS CONTENT -->).*?([ \t]*<!-- END AIRMASS CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided GOES-16 CONUS Air Mass RGB satellite image.
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
            "The interpretation is rendered below a live GOES-16 Air Mass RGB image between "
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


def build_airmass_html(interpretation: str, generated_at: str) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="airmass-heading">
  <h2 id="airmass-heading">Air Mass RGB</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{AIRMASS_RGB_URL}"
      alt="GOES-16 CONUS Air Mass RGB satellite image"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_airmass_content(interpretation: str, generated_at: str) -> dict:
    source = INDEX_HTML.read_text()
    html = build_airmass_html(interpretation, generated_at)
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


def run_tool(name: str, inputs: dict) -> dict:
    if name == "interpret_airmass_chart":
        return interpret_airmass_chart(**inputs)
    if name == "update_airmass_content":
        return update_airmass_content(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def fetch_airmass_image(url: str) -> tuple[str, str, int]:
    request = Request(url, headers={"User-Agent": "tlaloc-weather-bot/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ANTHROPIC_IMAGE_BYTES:
                raise ValueError(
                    f"Air Mass image size ({content_length} bytes) exceeds Anthropic's 5MB limit. "
                    "Unable to process this image."
                )

            media_type = response.headers.get_content_type()
            if not media_type.startswith("image/"):
                raise ValueError(f"Unexpected Air Mass content type: {media_type}")

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
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Failed to download Air Mass image from {url}: {exc}") from exc

    if not image_bytes:
        raise ValueError("Air Mass image download returned no data")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return encoded, media_type, len(image_bytes)


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    image_data, image_media_type, image_size = fetch_airmass_image(AIRMASS_RGB_URL)
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
                        "Interpret this GOES-16 CONUS Air Mass RGB satellite image and update the "
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
            result = run_tool(block.name, block.input)
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
