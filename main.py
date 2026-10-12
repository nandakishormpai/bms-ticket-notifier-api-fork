"""
BMS Ticket Checker — Multi-movie tracking via JSON config.
Emails initial discoveries & changes, followed by a clean summary table of active shows.
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
DEFAULT_TRACKS = [
    {
        "movie_name": "Jailer 2",
        "url": "https://in.bookmyshow.com/movies/hyderabad/jailer-2/ET00518674",
        "dates": "20261015,20261016,20261017,20261018,20261019",
        "theatre": "ALLU,AMB,PRASADS",
        "time_period": "",
        "languages": ["Telugu"],
    }
]

def load_tracks():
    raw = os.getenv("BMS_TRACKS", "")
    if raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse BMS_TRACKS JSON: {e}")
            sys.exit(1)
    return DEFAULT_TRACKS

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
    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None

def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
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
                        if ca != "0":  # Ignore sold-out categories
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
        if dates_set and s.date_code and s.date_code not in dates_set:
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
    with open(filename, "w") as f:
        json.dump(state, f, indent=2)

def build_state(shows, dates):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
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

    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        _, ico = AVAIL_STATUS_MAP.get(s["status"], ("AVAILABLE", "🟢"))
        changes.append(f"{ico} TICKETS AVAILABLE: {s['venue']} {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']}")

    return changes

# ──────────────────────────────────────────────────────────────────────
# HTML TABLE GENERATOR
# ──────────────────────────────────────────────────────────────────────
def generate_summary_table(shows):
    # Group shows by (Theatre, Date, Format)
    grouped = {}
    for s in shows:
        attr = (s.screen_attr or "Standard").strip()
        # Ignore Gold / Lounge formats
        if re.search(r'gold|lounge|lounger', attr, re.IGNORECASE):
            continue

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
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
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
        else:
            print(f"  ❌ Resend error {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")

# ──────────────────────────────────────────────────────────────────────
# MAIN EXECUTION LOOP
# ──────────────────────────────────────────────────────────────────────
def main():
    tracks = load_tracks()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running checks for {len(tracks)} movie(s)...")

    for track in tracks:
        url = track.get("url", "")
        parsed = parse_bms_url(url)
        event_code, region_slug = parsed["event_code"], parsed["region_slug"]
        movie_config_name = track.get("movie_name", "Movie")

        if not event_code or not region_slug:
            print(f"  ❌ Invalid URL for track: {movie_config_name}")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)
        raw_dates = track.get("dates", "").strip()
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()] if raw_dates else [parsed.get("date_code", "")]

        all_shows, all_dates = [], []
        movie_info = {"name": movie_config_name, "language": ""}

        for dc in date_list:
            data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
            if not data:
                continue
            if not movie_info["language"]:
                parsed_info = parse_movie_info(data)
                if parsed_info["name"] != "Unknown Movie":
                    movie_info["name"] = parsed_info["name"]
                movie_info["language"] = parsed_info["language"]
            all_dates.extend(parse_dates(data))
            all_shows.extend(parse_shows(data))

        if not all_shows:
            print(f"  ⚠️ No active showtimes found for {movie_info['name']}")
            continue

        filtered = filter_shows(all_shows, track, movie_info["language"])
        print(f"  🎬 {movie_info['name']} ({movie_info['language']}) — {len(filtered)} available show(s) matched filters.")

        state_file = get_state_filename(movie_config_name, event_code)
        new_state = build_state(filtered, all_dates)
        old_state = load_state(state_file)

        # First run: treat all matching active shows as initial discoveries
        if not old_state:
            changes = []
            for s in filtered:
                for c in s.categories:
                    _, ico = AVAIL_STATUS_MAP.get(c.status, ("AVAILABLE", "🟢"))
                    changes.append(f"{ico} INITIAL DISCOVERY: {s.venue_name} {s.time} [{s.date_code}] — {c.name} ₹{c.price}")
        else:
            changes = detect_changes(old_state, new_state)

        save_state(state_file, new_state)

        if changes:
            print(f"  ⚡ {len(changes)} update(s) detected! Sending email...")
            send_email(f"BMS Alert: {movie_info['name']} - {len(changes)} update(s)", changes, movie_info["name"], filtered)
        else:
            print("  ✅ No new available showtimes since last check.")

    print("\nAll checks complete.")

if __name__ == "__main__":
    main()
