"""Parent-link validation and scoped language/format selector traversal.

No catalogue HTML is requested. Only IDs inside this movie's format selector
are followed; recommendations and other page links are deliberately excluded.
"""
import json
import re
from urllib.parse import urlparse, unquote, urljoin

BASE = "https://in.bookmyshow.com"
EVENT = re.compile(r"\bET\d{8,}\b")
EVENT_URLS = {}


def parse_parent_url(url):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.netloc != "in.bookmyshow.com":
        raise ValueError("url must be an HTTPS in.bookmyshow.com movie link")
    if len(parts) < 4 or parts[0] != "movies" or not EVENT.fullmatch(parts[-1] if not parts[-1].isdigit() else parts[-2]):
        raise ValueError("Use a city-specific parent movie or buytickets link containing an ET event ID")
    event = next((part for part in parts if EVENT.fullmatch(part)), None)
    if not event:
        raise ValueError("Movie URL has no ET event ID")
    date = parts[-1] if re.fullmatch(r"\d{8}", parts[-1]) else ""
    return {"event_code": event, "region_slug": parts[1].lower(), "date_code": date}


def discover_events(track, city=None):
    """Seed the traversal directly from user input, without network requests."""
    url = track.get("url", "")
    parsed = parse_parent_url(url)
    EVENT_URLS.clear()
    EVENT_URLS[parsed["event_code"]] = url
    return [parsed["event_code"]]


def selector_events(payload):
    selector = payload.get("data", {}).get("bottomSheetData", {}).get("format-selector")
    if selector is None:
        return set()

    def remember(node):
        if isinstance(node, dict):
            for value in node.values():
                remember(value)
        elif isinstance(node, list):
            for value in node:
                remember(value)
        elif isinstance(node, str):
            url = urljoin(BASE, unquote(node).replace("\\/", "/"))
            try:
                parsed = parse_parent_url(url)
            except ValueError:
                return
            EVENT_URLS[parsed["event_code"]] = url

    remember(selector)
    return set(EVENT.findall(json.dumps(selector)))


def booking_url(event, date=""):
    url = EVENT_URLS.get(event, "")
    if not url:
        return ""  # No guessed slug when the selector exposes only eventCode.
    parts = urlparse(url).path.strip("/").split("/")
    index = parts.index(event)
    if "buytickets" not in parts:
        parts.insert(index, "buytickets")
        index += 1
    return BASE + "/" + "/".join(parts[:index + 1]) + ("/" + date if date else "")
