/**
 * Tlaloc — main.js
 *
 * Entry point for the Tlaloc weather site.
 * Wires up the UI and provides placeholder hooks for a real weather API.
 */

"use strict";

/* ============================================================
   Constants
   ============================================================ */

const DAYS_OF_WEEK = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// Placeholder forecast data — replace with real API responses.
const PLACEHOLDER_FORECAST = [
  { icon: "🌧️", high: 18, low: 12 },
  { icon: "⛅", high: 20, low: 13 },
  { icon: "☀️", high: 24, low: 15 },
  { icon: "🌤️", high: 22, low: 14 },
  { icon: "🌦️", high: 17, low: 11 },
];

/* ============================================================
   DOM helpers
   ============================================================ */

/**
 * Safely query a single element; throws if not found.
 * @param {string} selector
 * @param {Document | Element} [root=document]
 * @returns {Element}
 */
function $(selector, root = document) {
  const el = root.querySelector(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  return el;
}

/**
 * Create an element with optional class names and inner text.
 * @param {string} tag
 * @param {{ className?: string; text?: string; attrs?: Record<string,string> }} [options]
 * @returns {HTMLElement}
 */
function createElement(tag, { className, text, attrs = {} } = {}) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  return el;
}

/* ============================================================
   Weather rendering
   ============================================================ */

/**
 * Render the "current conditions" card.
 * @param {{ temp: number; description: string; humidity: number; wind: number }} data
 */
function renderCurrentWeather(data) {
  const card = $("#weather-card");
  card.innerHTML = "";

  const temp = createElement("p", {
    className: "weather-card__temp",
    text: `${data.temp}°C`,
  });

  const description = createElement("p", {
    className: "weather-card__description",
    text: data.description,
  });

  const detailsGrid = createElement("div", {
    className: "weather-card__details",
  });

  const details = [
    { label: "Humidity", value: `${data.humidity}%` },
    { label: "Wind", value: `${data.wind} km/h` },
  ];

  for (const { label, value } of details) {
    const item = createElement("div", { className: "weather-detail" });
    item.appendChild(
      createElement("p", { className: "weather-detail__label", text: label })
    );
    item.appendChild(
      createElement("p", { className: "weather-detail__value", text: value })
    );
    detailsGrid.appendChild(item);
  }

  card.appendChild(temp);
  card.appendChild(description);
  card.appendChild(detailsGrid);
}

/**
 * Render the forecast grid.
 * @param {Array<{ icon: string; high: number; low: number }>} days
 */
function renderForecast(days) {
  const grid = $("#forecast-grid");
  grid.innerHTML = "";

  const today = new Date();

  days.forEach((day, index) => {
    const date = new Date(today);
    date.setDate(today.getDate() + index + 1);
    const dayName = DAYS_OF_WEEK[date.getDay()];

    const item = createElement("div", { className: "forecast-item" });

    item.appendChild(
      createElement("p", { className: "forecast-item__day", text: dayName })
    );
    item.appendChild(
      createElement("p", {
        className: "forecast-item__icon",
        text: day.icon,
        attrs: { "aria-hidden": "true" },
      })
    );
    item.appendChild(
      createElement("p", {
        className: "forecast-item__temp-high",
        text: `${day.high}°`,
      })
    );
    item.appendChild(
      createElement("p", {
        className: "forecast-item__temp-low",
        text: `${day.low}°`,
      })
    );

    grid.appendChild(item);
  });
}

/**
 * Show an error message in the weather card.
 * @param {string} message
 */
function renderError(message) {
  const card = $("#weather-card");
  card.innerHTML = "";

  const alert = createElement("div", {
    className: "alert alert--error",
    text: message,
    attrs: { role: "alert" },
  });

  card.appendChild(alert);
}

/* ============================================================
   Data fetching
   ============================================================ */

/**
 * Fetch current weather for the given location.
 * Replace this stub with a real API call (e.g. Open-Meteo, OpenWeatherMap).
 *
 * @param {{ latitude: number; longitude: number }} _coords
 * @returns {Promise<{ temp: number; description: string; humidity: number; wind: number }>}
 */
async function fetchCurrentWeather(_coords) {
  // Stub — returns placeholder data after a short artificial delay.
  await new Promise((resolve) => setTimeout(resolve, 600));
  return { temp: 16, description: "light rain", humidity: 82, wind: 14 };
}

/**
 * Obtain the user's geolocation coordinates.
 * @returns {Promise<GeolocationCoordinates>}
 */
function getCoordinates() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("Geolocation is not supported by this browser."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) => resolve(position.coords),
      (err) => reject(new Error(`Geolocation error: ${err.message}`))
    );
  });
}

/* ============================================================
   App initialisation
   ============================================================ */

/**
 * Bootstrap the application.
 */
async function init() {
  // Render placeholder forecast immediately (no API key needed).
  renderForecast(PLACEHOLDER_FORECAST);

  try {
    const coords = await getCoordinates();
    const weather = await fetchCurrentWeather(coords);
    renderCurrentWeather(weather);
  } catch (err) {
    // Fall back to placeholder current conditions when location is unavailable.
    console.warn("Could not retrieve live weather:", err.message);
    try {
      const weather = await fetchCurrentWeather({});
      renderCurrentWeather(weather);
    } catch (fallbackErr) {
      renderError("Weather data is currently unavailable. Please try again later.");
      console.error("Weather fetch failed:", fallbackErr);
    }
  }
}

document.addEventListener("DOMContentLoaded", init);
