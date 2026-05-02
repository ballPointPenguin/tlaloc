import json
import urllib.request
from datetime import datetime

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic()

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
    }
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


def run_tool(name: str, inputs: dict) -> dict:
    if name == "get_weather":
        return get_weather(**inputs)
    raise ValueError(f"Unknown tool: {name}")


def main():
    location_name = "Mexico City, Mexico"
    latitude, longitude = 19.4326, -99.1332

    messages = [
        {
            "role": "user",
            "content": (
                f"What's the current weather in {location_name}? "
                "Fetch the data and write a concise, readable summary covering "
                "current conditions and a 3-day forecast."
            ),
        }
    ]

    # Turn 1: Claude decides to call get_weather
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        tools=TOOLS,
        messages=messages,
    )

    print(f"stop_reason: {response.stop_reason}")

    tool_use = next(b for b in response.content if b.type == "tool_use")
    print(f"tool call: {tool_use.name}({json.dumps(tool_use.input)})\n")

    tool_result = run_tool(tool_use.name, tool_use.input)
    print(f"tool result:\n{json.dumps(tool_result, indent=2)}\n")

    # Turn 2: send result back, Claude writes the report
    messages.append({"role": "assistant", "content": response.content})
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": json.dumps(tool_result),
            }
        ],
    })

    final = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        tools=TOOLS,
        messages=messages,
    )

    report = next(b.text for b in final.content if b.type == "text")
    print("--- Weather Report ---")
    print(report)
    print(f"\nGenerated: {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
