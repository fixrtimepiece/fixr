#!/usr/bin/env python3
"""
fixr_monitor.py

Watches Fixr organiser/venue listing pages (e.g. fixr.co/organiser/timepiece,
fixr.co/venue/1107), finds every event listed there, and checks whether each
one actually has tickets ON SALE right now (not just "the event page exists").

Sends a push notification via ntfy.sh the moment an event's tickets go from
not-yet-available to available. Run on a schedule (see the included GitHub
Actions workflow) -- each run is a single check, not a long-running loop.

State (what's been seen before) lives in state.json next to this script.

--- How ticket status is checked ---
Primary method: Fixr's internal JSON API (undocumented, used by their own
app -- not an official/public API, so it could change or break at any
time): GET https://api.fixr-app.com/api/v2/app/event/<id>
This returns each ticket type with sold_out / expired / not_yet_valid
flags -- much more precise than guessing from rendered text.

Fallback: if that API call fails (blocked, schema changed, etc.), fall
back to reading the individual event page's HTML for buy-signal vs
blocked-signal text. Less reliable (Fixr loads real pricing client-side
via JS), but better than nothing.
"""

import json
import re
import urllib.request
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
CONFIG_PATH = Path(__file__).parent / "config.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

API_HEADERS = {
    **HEADERS,
    "Accept": "application/json",
}

EVENT_API_URL = "https://api.fixr-app.com/api/v2/app/event/{id}"

# Matches links like https://fixr.co/event/boujee-2209-tickets-261866670
# or the relative form /event/boujee-2209-tickets-261866670
EVENT_LINK_RE = re.compile(
    r'href="([^"]*?/event/[a-z0-9\-]+?-tickets-(\d+))"', re.IGNORECASE
)

BUY_SIGNALS = ["get tickets", "buy tickets", "select tickets", "from £"]
BLOCKED_SIGNALS = ["sold out", "register interest", "join waitlist", "waitlist", "coming soon", "notify me"]


def fetch(url: str, headers=None) -> str:
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def strip_tags(html_fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", html_fragment).strip()


def clean_label(label: str) -> str:
    """Fixr's event cards often repeat the title twice back-to-back
    (mobile + desktop layout) when flattened to text. Collapse an exact
    repeated prefix if present."""
    n = len(label)
    for i in range(1, n // 2 + 1):
        if label[:i] == label[i:2 * i]:
            return (label[:i] + " — " + label[2 * i:]).strip(" —")
    return label


def find_events(listing_html: str):
    """Return {event_id: {"url":..., "label":...}} for every
    /event/...-tickets-<id> link found on a listing page."""
    events = {}
    for m in re.finditer(EVENT_LINK_RE, listing_html):
        href, event_id = m.group(1), m.group(2)
        url = href if href.startswith("http") else f"https://fixr.co{href}"

        start = listing_html.rfind("<a", 0, m.start())
        end = listing_html.find("</a>", m.end())
        label = strip_tags(listing_html[start:end]) if start != -1 and end != -1 else ""
        events[event_id] = {"url": url, "label": clean_label(label)}
    return events


def _find_key(d: dict, *candidates):
    for k in d:
        if k.lower().replace("_", "") in candidates:
            return d[k]
    return None


def status_via_api(event_id: str) -> str:
    """Query Fixr's internal event API. Returns 'available', 'blocked',
    'unknown' (couldn't determine -- caller should fall back)."""
    url = EVENT_API_URL.format(id=event_id)
    try:
        body = fetch(url, headers=API_HEADERS)
        data = json.loads(body)
    except Exception as e:
        print(f"[info] API check failed for event {event_id}: {e}")
        return "unknown"

    tickets = _find_key(data, "tickets") or data.get("tickets")
    if not tickets or not isinstance(tickets, list):
        return "unknown"

    any_purchasable = False
    for t in tickets:
        if not isinstance(t, dict):
            continue
        sold_out = bool(_find_key(t, "soldout"))
        expired = bool(_find_key(t, "expired"))
        not_yet_valid = bool(_find_key(t, "notyetvalid", "invalid"))
        if not sold_out and not expired and not not_yet_valid:
            any_purchasable = True
            break

    return "available" if any_purchasable else "blocked"


def status_via_page_text(event_url: str) -> str:
    """Fallback: crude text check on the event's own page."""
    try:
        html = fetch(event_url)
    except Exception as e:
        print(f"[warn] could not fetch event page {event_url}: {e}")
        return "unknown"
    text = html.lower()
    blocked = any(s in text for s in BLOCKED_SIGNALS)
    available = any(s in text for s in BUY_SIGNALS)
    if blocked and not available:
        return "blocked"
    if available and not blocked:
        return "available"
    return "unknown"


def get_status(event_id: str, event_url: str) -> str:
    status = status_via_api(event_id)
    if status == "unknown":
        status = status_via_page_text(event_url)
    return status


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def check_target(target: dict, state: dict) -> list:
    alerts = []
    name = target["name"]
    url = target["url"]

    try:
        listing_html = fetch(url)
    except Exception as e:
        print(f"[error] {name}: could not fetch listing page ({e})")
        return alerts

    events = find_events(listing_html)
    if not events:
        print(f"[warn] {name}: no event links found on listing page -- "
              f"the page structure may have changed, or the request was blocked.")

    target_state = state.setdefault(name, {})
    seen_events = target_state.setdefault("events", {})

    for event_id, info in events.items():
        status = get_status(event_id, info["url"])
        prev = seen_events.get(event_id)

        if prev is None:
            print(f"[info] {name}: first sighting of '{info['label']}' (status: {status})")
            if status == "available":
                alerts.append(f"🚨 TICKETS ON SALE — {name}: {info['label']}\n{info['url']}")
            seen_events[event_id] = {"label": info["label"], "status": status}
        else:
            if prev.get("status") != "available" and status == "available":
                alerts.append(f"🚨 TICKETS ON SALE — {name}: {info['label']}\n{info['url']}")
            prev["status"] = status
            prev["label"] = info["label"]

    return alerts


def notify(ntfy_topic: str, message: str):
    url = f"https://ntfy.sh/{ntfy_topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={"Title": "Fixr ticket alert", "Priority": "urgent", "Tags": "rotating_light"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    config = json.loads(CONFIG_PATH.read_text())
    ntfy_topic = config["ntfy_topic"]

    if "CHANGE-ME" in ntfy_topic:
        print("!! Set a unique ntfy_topic in config.json before relying on this. !!")

    state = load_state()
    all_alerts = []
    for target in config["targets"]:
        all_alerts.extend(check_target(target, state))

    save_state(state)

    for alert in all_alerts:
        print("ALERT:", alert)
        try:
            notify(ntfy_topic, alert)
        except Exception as e:
            print(f"[error] failed to send notification: {e}")

    if not all_alerts:
        print("No changes detected.")


if __name__ == "__main__":
    main()
