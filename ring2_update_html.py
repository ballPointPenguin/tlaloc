import json
import re
import urllib.request
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

INDEX_HTML = Path(__file__).parent / "index.html"

SENTINEL_RE = re.compile(
    r"([ \t]*<!-- BEGIN WEATHER CONTENT -->).*?([ \t]*<!-- END WEATHER CONTENT -->)",
    re.DOTALL,
)

SYSTEM = """\
You generate weather content for the Tlaloc weather page (a static GitHub Pages site).

When asked to update the page:
1. Call get_weather for the requested location.
2. Call update_weather_content with the generated HTML and an ISO 8601 timestamp.

WMO weather code → emoji reference:
  0=☀️  1=🌤️  2=⛅  3=☁️  45/48=🌫️
  51-55=🌦️  61-65=🌧️  66-67=🌨️  71-77=❄️
  80-82=🌦️  85-86=🌨️  95=⛈️  96/99=⛈️
"""

TOOLS = [
    {
        "name": "get_weather",
        "description": (
            "Fetch current weather conditions and a 3-day forecast for a location "
            "using the Open-Meteo API. Returns temperature, feels-like, humidity, "
            "wind, precipitation, and daily highs/lows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number", "description": "Latitude of the location"},
                "longitude": {"type": "number", "description": "Longitude of the location"},
                "location_name": {
                    "type": "string",
                    "description": "Human-readable name for display in the report",
                },
            },
            "required": ["latitude", "longitude", "location_name"],
        },
    },
    {
        "name": "update_weather_content",
        "description": (
            "Replace the weather content block in index.html. "
            "The html argument is spliced between <!-- BEGIN WEATHER CONTENT --> "
            "and <!-- END WEATHER CONTENT --> markers. "
            "Use these CSS classes: "
            "weather-card, weather-card__temp, weather-card__description, "
            "weather-card__details, weather-detail, weather-detail__label, "
            "weather-detail__value, forecast-grid, forecast-item, "
            "forecast-item__day, forecast-item__icon, "
            "forecast-item__temp-high, forecast-item__temp-low."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "Full HTML for both sections (current conditions and forecast). "
                        "Use two <section aria-labelledby=...> elements with <h2> headings. "
                        "Include a small 'Updated: <time datetime=...>' note."
                    ),
                },
                "generated_at": {
                    "type": "string",
                    "description": "ISO 8601 timestamp of when the weather data was fetched",
                },
            },
            "required": ["html", "generated_at"],
        },
    },
]


def get_weather(latitude: float, longitude: float, location_name: str) -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m"
        ",wind_speed_10m,wind_direction_10m,weather_code,precipitation"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
        "&forecast_days=3"
    )
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    return {
        "location": location_name,
        "current": data["current"],
        "current_units": data["current_units"],
        "daily": data.get("daily"),
        "daily_units": data.get("daily_units"),
    }


def update_weather_content(html: str, generated_at: str) -> dict:
    source = INDEX_HTML.read_text()
    replacement = f"<!-- BEGIN WEATHER CONTENT -->\n{html}\n        <!-- END WEATHER CONTENT -->"
    updated, count = SENTINEL_RE.subn(replacement, source)
    if count == 0:
        return {"success": False, "error": "Sentinel comments not found in index.html"}
    INDEX_HTML.write_text(updated)
    return {"success": True, "path": str(INDEX_HTML), "generated_at": generated_at}


def run_tool(name: str, inputs: dict) -> dict:
    if name == "get_weather":
        return get_weather(**inputs)
    if name == "update_weather_content":
        return update_weather_content(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def main():
    messages = [
        {
            "role": "user",
            "content": (
                "Fetch the current weather for Mexico City, Mexico "
                "(latitude 19.4326, longitude -99.1332) "
                "and update the Tlaloc weather page with the current conditions "
                "and 3-day forecast."
            ),
        }
    ]

    # Agentic loop — runs until Claude stops requesting tool calls
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        print(f"stop_reason: {response.stop_reason}")

        if response.stop_reason != "tool_use":
            break

        # Collect and execute all tool calls in this turn
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

    # Final text response from Claude
    for block in response.content:
        if block.type == "text":
            print("---")
            print(block.text)


if __name__ == "__main__":
    main()
