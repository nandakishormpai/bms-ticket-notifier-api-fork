"""
BMS Ticket Checker — Multi-movie tracking via JSON config.
Emails initial discoveries & changes, followed by a clean summary table of active shows.
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from urllib.parse import urlparse
from discovery import discover_events, selector_events, booking_url
import argparse
import hashlib
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
def load_tracks():
    raw = os.getenv("BMS_TRACKS", "").strip()
    if raw:
        tracks = json.loads(raw)
    else:
        with open("bms_tracks.json", encoding="utf-8") as config:
            tracks = json.load(config)
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("BMS_TRACKS must be a nonempty JSON array")
    for track in tracks:
        if not track.get("movie_name"):
            raise ValueError("Every track needs movie_name")
        city = track.get("city") or parse_bms_url(track.get("url", ""))["region_slug"]
        if city not in REGION_MAP:
            raise ValueError(f"Unsupported city {city!r}; supported: {', '.join(REGION_MAP)}")
        track["city"] = city
        for dc in track.get("dates", "").split(","):
            if dc.strip():
                datetime.strptime(dc.strip(), "%Y%m%d")
        for period in track.get("time_period", "").split(","):
            if period.strip() and period.strip().lower() not in TIME_PERIODS:
                raise ValueError(f"Unknown time period: {period}")
    return tracks

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "aviiciii@resend.dev")

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS & MAPS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":   ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":    ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":     ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore": ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":   ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":      ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":     ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}

@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)
    event_code: str = ""
    language: str = ""
    booking_url: str = ""
    movie_format: str = ""

@dataclass
class DateInfo:
    date_code: str
    status: str

# ──────────────────────────────────────────────────────────────────────
# HELPERS & PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result

def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")

API_URL = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"

def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "lat": lat, "lon": lon,
    }
    resp = requests.get(API_URL, headers=headers, params=params, timeout=25)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload.get("data"), dict) or "showtimeWidgets" not in payload["data"]:
        raise ValueError("Unrecognized showtimes response; refusing to replace state")
    return payload

def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if re.search(r"[•·]|\s-\s", c.get("text", "")):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info

def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates

def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(sa.get("showDateCode", "") or sa.get("dateCode", "")).strip()
                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "") or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        if ca in AVAIL_STATUS_MAP:  # Only known bookable categories
                            show.categories.append(CatInfo(
                                name=cat.get("priceDesc", ""),
                                price=cat.get("curPrice", "0"),
                                status=ca,
                            ))
                    if show.categories:
                        shows.append(show)
    return shows

def filter_shows(shows, track, movie_language):
    result = []
    kws = [k.strip().lower() for k in track.get("theatre", "").split(",") if k.strip()]
    periods = [p.strip().lower() for p in track.get("time_period", "").split(",") if p.strip()]
    dates_set = set(d.strip() for d in track.get("dates", "").split(",") if d.strip())
    langs = [l.strip().lower() for l in track.get("languages", []) if l.strip()]

    if langs:
        if not any(l in movie_language.lower() for l in langs):
            return []

    for s in shows:
        if kws and not any(k in s.venue_name.lower() for k in kws):
            continue
        if dates_set and s.date_code not in dates_set:
            continue
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            if not any(lo <= tc < hi for p in periods if p in TIME_PERIODS for lo, hi in [TIME_PERIODS[p]]):
                continue
        result.append(s)
    return result

# ──────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────────────────────────────
def get_state_filename(movie_name, event_code):
    sanitized_name = re.sub(r'[^a-zA-Z0-9]', '_', movie_name.lower())
    sanitized_name = re.sub(r'_+', '_', sanitized_name).strip('_')
    return f"bms_state_{sanitized_name}_{event_code}.json"

def load_state(filename):
    try:
        with open(filename) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(filename, state):
    temporary = filename + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(temporary, filename)

def build_state(shows, dates):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.event_code}|{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
                "language": s.language,
                "format": s.movie_format or s.screen_attr,
                "url": s.booking_url,
            }
    date_state = {d.date_code: d.status for d in dates}
    return {"shows": show_state, "dates": date_state}

def detect_changes(old_state, new_state):
    changes = []
    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        if old_dates.get(dc) == "NOT_OPEN" and status in ("BOOKABLE", "AVAILABLE"):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    for key in sorted(set(new_shows) - set(old_shows)):
        s = new_shows[key]
        _, ico = AVAIL_STATUS_MAP.get(s["status"], ("AVAILABLE", "🟢"))
        changes.append(f"{ico} TICKETS AVAILABLE: {s['venue']} {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']} — {s.get('language', '')} {s.get('format', '')} {s.get('url', '')}")

    return changes

# ──────────────────────────────────────────────────────────────────────
# HTML TABLE GENERATOR
# ──────────────────────────────────────────────────────────────────────
def generate_summary_table(shows):
    # Group shows by (Theatre, Date, Format)
    grouped = {}
    for s in shows:
        attr = " / ".join(x for x in (s.language, s.movie_format, s.screen_attr or "Standard") if x)

        key = (s.venue_name, s.date_code, attr)
        if key not in grouped:
            grouped[key] = set()
        grouped[key].add(s.time)

    if not grouped:
        return ""

    table_rows = []
    for (venue, date_code, fmt), times in sorted(grouped.items()):
        formatted_date = date_code
        if len(date_code) == 8:
            try:
                dt = datetime.strptime(date_code, "%Y%m%d")
                formatted_date = dt.strftime("%d %b %Y")
            except ValueError:
                pass

        time_str = ", ".join(sorted(times))
        table_rows.append(f"""
        <tr>
            <td style="padding: 8px; border: 1px solid #ddd;"><b>{escape(venue)}</b></td>
            <td style="padding: 8px; border: 1px solid #ddd; white-space: nowrap;">{escape(formatted_date)}</td>
            <td style="padding: 8px; border: 1px solid #ddd;">{escape(time_str)}</td>
            <td style="padding: 8px; border: 1px solid #ddd;">{escape(fmt)}</td>
        </tr>""")

    return f"""
    <h3 style="font-size: 15px; margin-top: 24px; margin-bottom: 8px;">Active Shows Summary</h3>
    <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;">
        <thead>
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 8px; border: 1px solid #ddd;">Theatre</th>
                <th style="padding: 8px; border: 1px solid #ddd;">Date</th>
                <th style="padding: 8px; border: 1px solid #ddd;">Timings</th>
                <th style="padding: 8px; border: 1px solid #ddd;">Format</th>
            </tr>
        </thead>
        <tbody>
            {"".join(table_rows)}
        </tbody>
    </table>"""

# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION
# ──────────────────────────────────────────────────────────────────────
def send_email(subject, changes, movie_name, shows):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️ Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return False

    # Define IST timezone (+5:30)
    IST = timezone(timedelta(hours=5, minutes=30))
    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    rows = "".join(f'<li style="padding:4px 0;font-size:14px;">{escape(c)}</li>' for c in changes)
    summary_table_html = generate_summary_table(shows)
    
    html = f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#333;line-height:1.4;">
    <h2 style="margin:0 0 4px 0;font-size:18px;">BMS Alert: {escape(movie_name)}</h2>
    <p style="margin:0 0 16px 0;font-size:12px;color:#666;">{escape(now_str)}</p>
    
    <h3 style="font-size:15px;margin-bottom:8px;">Updates / Discoveries</h3>
    <ul style="padding-left:20px;line-height:1.5;">{rows}</ul>

    {summary_table_html}

    <p style="font-size:12px;color:#999;margin-top:24px;">Automated alert from BMS Ticket Notifier.</p>
</body>
</html>"""

    plain = f"{subject}\n\nUpdates:\n" + "\n".join(f"- {c}" for c in changes)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": frm, "to": [to], "subject": subject, "text": plain, "html": html},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
            return True
        else:
            print(f"  ❌ Resend error {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
    return False

# ──────────────────────────────────────────────────────────────────────
# MAIN EXECUTION LOOP
# ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Discover and fetch; do not email or write state")
    parser.add_argument("--discover-only", action="store_true", help="Print matching movie IDs only; no email/state")
    args = parser.parse_args()
    failures = 0
    for track in load_tracks():
        try:
            city = track["city"]
            region = resolve_region(city)
            events = discover_events(track, region[1])
            print(f"{track['movie_name']} / {city}: discovered {events}")
            if args.discover_only:
                continue
            pending = list(events)
            visited = set()
            all_shows, all_dates = [], []
            dates = [d.strip() for d in track.get("dates", "").split(",") if d.strip()] or [""]
            today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y%m%d")
            dates = [d for d in dates if not d or d >= today]
            while pending and dates:
                event = pending.pop(0)
                if event in visited:
                    continue
                visited.add(event)
                if len(visited) > 60:
                    raise ValueError("Unexpectedly many format variants; inspect selector schema")
                event_dates = list(dates)
                fetched_dates = set()
                while event_dates:
                    dc = event_dates.pop(0)
                    if dc in fetched_dates:
                        continue
                    fetched_dates.add(dc)
                    data = fetch_bms(event, dc, *region)
                    for related in selector_events(data):
                        if related not in visited and related not in pending:
                            pending.append(related)
                    if dates == [""]:
                        for date in parse_dates(data):
                            if date.status in ("BOOKABLE", "AVAILABLE") and date.date_code >= today and date.date_code not in fetched_dates:
                                event_dates.append(date.date_code)
                    info = parse_movie_info(data)
                    if track.get("languages") and not info["language"]:
                        raise ValueError(f"Missing language metadata for {event}; cannot safely apply filter")
                    shows = parse_shows(data)
                    for show in shows:
                        # Requested date is authoritative only when API omits it.
                        selected_date = next((d.date_code for d in parse_dates(data) if d.status == "BOOKABLE"), "")
                        show.date_code = show.date_code or dc or selected_date
                        if not show.date_code:
                            raise ValueError("Show has no date; refusing undated alert")
                        show.event_code = event
                        metadata = re.split(r"\s*[•·]\s*|\s+-\s+", info["language"], maxsplit=1)
                        show.language = metadata[0]
                        show.movie_format = metadata[1] if len(metadata) > 1 else ""
                        show.booking_url = booking_url(event, show.date_code)
                        show.screen_attr = show.screen_attr if isinstance(show.screen_attr, str) else json.dumps(show.screen_attr)
                    all_shows.extend(filter_shows(shows, track, info["language"]))
                    all_dates.extend(d for d in parse_dates(data) if not dc or d.date_code == dc)
            unique = {(s.event_code, s.venue_code, s.session_id, s.date_code): s for s in all_shows}
            shows = list(unique.values())
            print(f"  {len(visited)} variants checked; {len(shows)} bookable shows matched")
            for show in shows:
                print(f"  {show.date_code} {show.venue_name} {show.time} {show.screen_attr} {show.booking_url}")
            if args.dry_run or not dates:
                continue
            # Config fingerprint keeps filters/cities from sharing state accidentally.
            identity = json.dumps(track, sort_keys=True, ensure_ascii=True)
            key = hashlib.sha256(identity.encode()).hexdigest()[:12]
            filename = get_state_filename(track["movie_name"], key)
            old = load_state(filename)
            current = build_state(shows, all_dates)
            changes = detect_changes(old, current)
            if changes:
                if not send_email(f"BMS Alert: {track['movie_name']} - {len(changes)} updates", changes, track["movie_name"], shows):
                    raise RuntimeError("Notification not delivered; state kept for retry")
            save_state(filename, current)
        except (requests.RequestException, ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            failures += 1
            print(f"ERROR {track['movie_name']}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
