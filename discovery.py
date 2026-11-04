"""Discover actual BMS links; never calculate or guess ET IDs.

The website's private response schema is deliberately isolated here.
"""
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse, unquote
import requests

BASE = "https://in.bookmyshow.com"
EVENT = re.compile(r"\bET\d{8,}\b")
EVENT_URLS = {}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.label = []
        self.scripts = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href = attrs.get("href", "")
            self.label = [attrs.get("title", ""), attrs.get("aria-label", "")]
        elif tag == "img" and self.href is not None:
            self.label.append(attrs.get("alt", ""))
        elif tag == "script":
            self.script = []

    def handle_data(self, text):
        if self.href is not None:
            self.label.append(text)
        if self.script is not None:
            self.script.append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join(self.label)))
            self.href = None
        elif tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None


def title_match(title, track):
    """Require every query word, in any order; allow additional title words."""
    actual_words = set(normalize(title).split())
    names = [track["movie_name"], *track.get("search_names", [])]
    for name in names:
        query_words = set(normalize(name).split())
        if query_words and query_words.issubset(actual_words):
            return True
    return False


def movie_link(href):
    href = unquote(unescape(href)).replace("\\/", "/")
    parsed = urlparse(href)
    if parsed.netloc and parsed.netloc != "in.bookmyshow.com":
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "movies":
        return None
    match = EVENT.search(parsed.path)
    if not match:
        return None
    index = parts.index(match.group())
    slug_index = index - 2 if parts[index - 1] == "buytickets" else index - 1
    return match.group(), parts[slug_index].replace("-", " ")


def json_links(node):
    """Only pair a title with URL fields in the same record, never a parent page."""
    if isinstance(node, dict):
        title = next((node[k] for k in ("title", "name", "eventTitle", "eventName") if isinstance(node.get(k), str)), "")
        for key, value in node.items():
            if isinstance(value, str) and ("url" in key.lower() or "link" in key.lower()):
                if movie_link(value):
                    yield value, title
            elif isinstance(value, (dict, list)):
                yield from json_links(value)
    elif isinstance(node, list):
        for value in node:
            yield from json_links(value)


def extract_candidates(html, track):
    parser = Links()
    parser.feed(html)
    links = list(parser.links)
    for script in parser.scripts:
        try:
            links.extend(json_links(json.loads(script)))
        except (ValueError, TypeError):
            pass
    # Hydration assignments are JavaScript rather than JSON. Only extract movie
    # URL strings there; matching still requires the exact movie slug.
    for script in parser.scripts:
        decoded = script.replace("\\/", "/").replace("\\u002F", "/")
        for href in re.findall(r"(?:https://in\.bookmyshow\.com)?/movies/[^\s\"'<>\\]+", decoded):
            links.append((href, ""))
    events = set()
    for href, label in links:
        parsed = movie_link(href)
        if parsed and (title_match(label, track) or title_match(parsed[1], track)):
            events.add(parsed[0])
            path = urlparse(unquote(unescape(href)).replace("\\/", "/")).path
            EVENT_URLS[parsed[0]] = BASE + path
    return events


def get_html(url):
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    html = response.text
    if "<html" not in html.lower() or any(x in html.lower() for x in ("verify you are human", "just a moment...", "access denied")):
        raise ValueError(f"Blocked or unexpected HTML at {url}")
    return html


def discover_events(track, city):
    # City movie catalogue acts as a local title index. Language catalogues expose
    # editions which may not be visible in the first list of recommended films.
    urls = [f"{BASE}/explore/movies-{city}", f"{BASE}/explore/upcoming-movies-{city}"]
    urls.extend(f"{BASE}/explore/{language.lower()}-movies-{city}" for language in track.get("languages", []))
    found = set()
    errors = []
    for url in urls:
        try:
            found.update(extract_candidates(get_html(url), track))
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    # A supplied legacy URL is an extra seed, never the only discovery method.
    seed = movie_link(track.get("url", ""))
    if seed:
        found.add(seed[0])
        EVENT_URLS[seed[0]] = track["url"]
    if not found:
        raise ValueError("Movie not discovered in city catalogues; check search words/search_names. " + "; ".join(errors))
    for error in errors:
        print(f"  Discovery warning: {error}")
    return sorted(found)


def selector_events(payload):
    """Only crawl this movie's format selector, not recommendations or ads.

    Selector actions can contain eventCode or booking URLs. Inspecting the scoped
    subtree supports both without hardcoding undocumented nested action shapes.
    """
    selectors = payload.get("data", {}).get("bottomSheetData", {})
    selector = selectors.get("format-selector")
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
            link = movie_link(node)
            if link:
                EVENT_URLS[link[0]] = BASE + urlparse(node).path
    remember(selector)
    return set(EVENT.findall(json.dumps(selector)))


def booking_url(event, date):
    url = EVENT_URLS.get(event, "")
    if not url:
        return ""  # Do not invent a movie slug when only an eventCode was exposed.
    parts = urlparse(url).path.strip("/").split("/")
    index = parts.index(event)
    if "buytickets" not in parts:
        parts.insert(index, "buytickets")
        index += 1
    return BASE + "/" + "/".join(parts[:index + 1]) + "/" + date

