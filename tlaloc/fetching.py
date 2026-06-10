"""HTTP helpers with the failure handling the rest of the pipeline relies on.

Every function raises SourceError on anything unexpected — dead links,
non-image content types, oversized payloads, malformed JSON. Collectors catch
SourceError (and anything else) and turn it into a failed SourceReport, so one
flaky upstream never kills the run.
"""

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import HTTP_TIMEOUT_SECONDS, MAX_IMAGE_BYTES, PROBE_TIMEOUT_SECONDS, USER_AGENT


class SourceError(Exception):
    """An external data source did not behave as expected."""


def _request(url: str, accept: str | None = None) -> Request:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return Request(url, headers=headers)


def fetch_image_base64(url: str) -> tuple[str, str]:
    """Download an image, returning (base64_data, media_type).

    Raises SourceError on network failure, non-image content, or payloads
    over the Anthropic 5 MB image limit.
    """
    try:
        with urlopen(_request(url), timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if not media_type.startswith("image/"):
                raise SourceError(f"Expected image content from {url}, got {content_type!r}")

            data = bytearray()
            while chunk := response.read(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise SourceError(f"Image at {url} exceeds {MAX_IMAGE_BYTES} byte limit")
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise SourceError(f"Failed to download image {url}: {exc}") from exc

    if not data:
        raise SourceError(f"Image download from {url} returned no data")
    return base64.b64encode(data).decode("ascii"), media_type


def fetch_text(url: str, max_chars: int) -> str:
    """Download a text document, truncated to max_chars."""
    try:
        with urlopen(_request(url), timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(max_chars * 4)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise SourceError(f"Failed to fetch text {url}: {exc}") from exc

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise SourceError(f"Text fetch from {url} returned no content")
    return text[:max_chars]


def fetch_json(url: str) -> dict:
    """Fetch and parse a JSON document."""
    try:
        with urlopen(_request(url, accept="application/ld+json"), timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise SourceError(f"Failed to fetch JSON {url}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"Response from {url} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceError(f"Expected a JSON object from {url}, got {type(data).__name__}")
    return data


def image_url_exists(url: str) -> bool:
    """Return True when the URL is reachable and serves image content.

    A 404 is an expected miss while probing timestamped chart URLs; any other
    failure is also treated as a miss so probing can continue to the next
    candidate rather than aborting the whole source.
    """
    try:
        with urlopen(_request(url), timeout=PROBE_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            return content_type.split(";", 1)[0].strip().lower().startswith("image/")
    except (HTTPError, URLError, OSError, TimeoutError):
        return False
