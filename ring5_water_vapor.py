import base64
import json
import re
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"
WATER_VAPOR_URL = "https://cdn.star.nesdis.noaa.gov/GOES16/ABI/CONUS/10/latest.jpg"

WATER_VAPOR_SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN WATER VAPOR CONTENT -->).*?([ \t]*<!-- END WATER VAPOR CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate synoptic meteorology content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Review the provided GOES-16 water vapor satellite image (Band 10, 7.3 µm lower-level water vapor).
2. Identify the most important visible upper- and mid-level features: shortwave troughs and ridges,
   areas of dry intrusion (dark regions), the overall flow pattern (amplified/meridional vs. zonal),
   any PV streamers digging equatorward, and jet stream position implied by moisture gradients.
3. Call interpret_water_vapor_chart with a brief 2-4 sentence interpretation grounded in the image.
4. Call update_water_vapor_content with the interpretation text and the provided ISO 8601 timestamp.

Keep the interpretation concise, meteorological, and cautious. Mention only features that are
clearly visible in the image. Use plain text, not markdown.
"""

TOOLS = [
    {
        "name": "interpret_water_vapor_chart",
        "description": (
            "Return your brief interpretation of the provided water vapor satellite image. "
            "Summarize the most important visible features: shortwave troughs, dry intrusions, "
            "flow amplification, and any PV streamers in 2-4 sentences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interpretation": {
                    "type": "string",
                    "description": "Brief plain-text interpretation of the water vapor image",
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
        "name": "update_water_vapor_content",
        "description": (
            "Replace the water vapor content block in index.html. "
            "The interpretation is rendered below a live GOES-16 water vapor image between "
            "<!-- BEGIN WATER VAPOR CONTENT --> and <!-- END WATER VAPOR CONTENT --> markers. "
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


def fetch_water_vapor_image() -> bytes:
    with urllib.request.urlopen(WATER_VAPOR_URL) as resp:
        return resp.read()


def format_timestamp(generated_at: str) -> str:
    dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%B')} {dt.day}, {dt.year} at {time_text} UTC"


def interpret_water_vapor_chart(interpretation: str, generated_at: str) -> dict:
    normalized = " ".join(interpretation.split())
    return {
        "success": True,
        "interpretation": normalized,
        "generated_at": generated_at,
    }


def build_water_vapor_html(interpretation: str, generated_at: str) -> str:
    safe_interpretation = escape(" ".join(interpretation.split()))
    human_timestamp = format_timestamp(generated_at)
    return f"""<section aria-labelledby="water-vapor-heading">
  <h2 id="water-vapor-heading">Water Vapor</h2>
  <div class="synoptic-card">
    <img
      class="synoptic-card__image"
      src="{WATER_VAPOR_URL}"
      alt="GOES-16 CONUS lower-level water vapor satellite image (Band 10, 7.3 µm)"
    />
    <div class="synoptic-card__body">
      <p class="synoptic-card__text">{safe_interpretation}</p>
      <p class="synoptic-card__timestamp">
        <small>Interpreted: <time datetime="{generated_at}">{human_timestamp}</time></small>
      </p>
    </div>
  </div>
</section>"""


def update_water_vapor_content(interpretation: str, generated_at: str) -> dict:
    source = INDEX_HTML.read_text()
    html = build_water_vapor_html(interpretation, generated_at)
    replacement = (
        "        <!-- BEGIN WATER VAPOR CONTENT -->\n"
        f"{html}\n"
        "        <!-- END WATER VAPOR CONTENT -->"
    )
    updated, count = WATER_VAPOR_SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "Water vapor sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict) -> dict:
    if name == "interpret_water_vapor_chart":
        return interpret_water_vapor_chart(**inputs)
    if name == "update_water_vapor_content":
        return update_water_vapor_content(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def main():
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    image_bytes = fetch_water_vapor_image()
    image_data = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Interpret this GOES-16 CONUS water vapor satellite image and update the "
                        "Tlaloc page with a brief synoptic summary. Focus on shortwave troughs, "
                        "dry intrusions, flow amplification, and any PV streamers. Use this exact "
                        f"ISO 8601 timestamp when calling tools: {generated_at}"
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
