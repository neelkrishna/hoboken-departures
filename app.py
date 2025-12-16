import re
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Hoboken Departures", layout="wide")

# -----------------------------
# Data sources
# PATH realtime (community API): https://path.api.razza.dev/v1/stations/hoboken/realtime
# NJT MyBus HTML pages (official mobile site):
#   11th+Washington stop #: 20513
#   10th+Washington stop #: 20516
# -----------------------------

PATH_URL = "https://www.panynj.gov/bin/portauthority/ridepath.json"

# These are the direct "ETA" pages (HTML) from NJT MyBus.
# You can swap direction/route/showAllBusses as you like.
MYBUS_11TH_WASH = (
    "https://mybusnow.njtransit.com/bustime/wireless/html/eta.jsp"
    "?direction=New+York&id=20513&route=126&showAllBusses=on"
)

MYBUS_10TH_WASH = (
    "https://mybusnow.njtransit.com/bustime/wireless/html/eta.jsp"
    "?direction=Hoboken%2FJersey+City&id=20516&route=126&showAllBusses=on"
)

DEFAULT_MAX_WINDOW_MIN = 120

def format_time_from_minutes(minutes: int):
    """
    Returns (time_str, minutes) where time_str is 'HH:MM am/pm'
    based on current local time plus `minutes`.
    """
    now_local = datetime.now()
    arrival = now_local + timedelta(minutes=minutes)
    return arrival.strftime("%I:%M %p").lstrip("0")

def _get_json(url: str, timeout=10):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


SESSION = requests.Session()

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MYBUS_HOME = "https://mybusnow.njtransit.com/bustime/wireless/html/home.jsp"

def _get_html(url: str, timeout=10):
    # hit home first to establish cookies
    SESSION.get(MYBUS_HOME, timeout=timeout, headers=BROWSER_HEADERS)

    # then fetch ETA with a referer
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = MYBUS_HOME

    r = SESSION.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text


def parse_path_realtime():
    data = _get_json(PATH_URL)

    # ridepath.json structure: results[] -> destinations[] -> messages[]
    # We filter for Hoboken station code: "HOB"
    rows = []

    now = datetime.now(timezone.utc)

    for station in data.get("results", []):
        if station.get("consideredStation") != "HOB":
            continue

        for dest in station.get("destinations", []):
            for msg in dest.get("messages", []):
                # arrivalTime is often an ISO-ish timestamp; arrivalTimeMessage is human text.
                arrival_iso = msg.get("arrivalTime")
                headsign = msg.get("headSign") or dest.get("label") or "PATH"
                line = msg.get("lineName") or msg.get("line") or "PATH"

                minutes = None
                if arrival_iso:
                    try:
                        # Example formats vary; handle common ISO with Z / offset
                        ts = arrival_iso.replace("Z", "+00:00")
                        arrival = datetime.fromisoformat(ts)
                        delta = arrival - now
                        minutes = max(0, int(delta.total_seconds() // 60))
                    except Exception:
                        minutes = None

                # If we can't parse arrivalTime, fall back to arrivalTimeMessage like "5 min"
                if minutes is None:
                    m = re.search(r"(\d+)\s*min", str(msg.get("arrivalTimeMessage", "")), re.I)
                    if m:
                        minutes = int(m.group(1))

                if minutes is None:
                    continue

                if 0 <= minutes <= MAX_WINDOW_MIN:
                    rows.append({"line": str(line), "to": str(headsign), "minutes": minutes, "raw": msg})

    # sort and dedupe
    uniq = {(r["line"], r["to"], r["minutes"]): r for r in rows}
    rows = sorted(uniq.values(), key=lambda x: x["minutes"])
    return rows

def parse_njt_mybus(url: str):
    """
    Scrapes NJT MyBus HTML ETA page.
    Returns list of dicts:
      { "route": str, "to": str, "minutes": int }
    Filters to <= MAX_WINDOW_MIN minutes when possible.
    """
    html = _get_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # The page typically shows blocks like:
    # "#126 To 126 NEW YORK 13 MIN" or "< 1 MIN" or "DUE"
    text = soup.get_text("\n", strip=True)

    # Regex to capture route + destination + ETA
    # Examples:
    #  "#126  To 126 NEW YORK   13 MIN"
    #  "#126  To 126 HOBOKEN-PATH   < 1 MIN"
    #  "#22   To 22 HOBOKEN   DUE"
    pattern = re.compile(
        r"#(?P<route>\d+)\s+To\s+(?P<to>.+?)\s+(?P<eta>(?:<\s*1|DUE|\d+))\s*(?:MIN)?",
        re.IGNORECASE,
    )

    out = []
    for m in pattern.finditer(text):
        route = m.group("route").strip()
        to = m.group("to").strip()
        eta_raw = m.group("eta").strip().upper()

        if eta_raw == "DUE" or eta_raw.startswith("<"):
            minutes = 0
        else:
            try:
                minutes = int(eta_raw)
            except ValueError:
                continue

        if 0 <= minutes <= MAX_WINDOW_MIN:
            out.append({"route": route, "to": to, "minutes": minutes})

    # If the page contains duplicates, keep unique by (route,to,minutes)
    uniq = {}
    for r in out:
        uniq[(r["route"], r["to"], r["minutes"])] = r
    out = list(uniq.values())
    out.sort(key=lambda x: x["minutes"])
    return out


def render_list(title, rows, kind="path"):
    st.subheader(title)
    if not rows:
        st.info("No upcoming departures found (or source temporarily unavailable).")
        return

    for r in rows:
        time_str = format_time_from_minutes(r["minutes"])
        if kind == "path":
            st.write(
                f"**{r['line']}** → {r['to']}  \n"
                f"🕒 {time_str} ({r['minutes']} min)"
            )
        else:
            st.write(
                f"**#{r['route']}** → {r['to']}  \n"
                f"🕒 {time_str} ({r['minutes']} min)"
            )


# -----------------------------
# UI
# -----------------------------
with st.sidebar:
    max_window_min = st.slider(
        "Show departures within (minutes)",
        30,
        240,
        DEFAULT_MAX_WINDOW_MIN,
        15,
    )
    refresh_sec = st.slider("Auto-refresh (seconds)", 15, 120, 30, 5)
    st.caption("Tip: leave this running on a second monitor.")

MAX_WINDOW_MIN = max_window_min

st.title(f"Hoboken Departures (Next {MAX_WINDOW_MIN} Minutes)")

now_et = datetime.now(timezone(timedelta(hours=-5)))  # America/New_York standard offset
st.caption(f"Last updated: **{now_et.strftime('%Y-%m-%d %I:%M:%S %p')} ET**")

col1, col2, col3 = st.columns(3)

try:
    with col1:
        path_rows = parse_path_realtime()
        render_list("PATH — Hoboken", path_rows, kind="path")
except Exception as e:
    with col1:
        st.subheader("PATH — Hoboken")
        st.error(f"PATH fetch failed: {e}")

try:
    with col2:
        bus11 = parse_njt_mybus(MYBUS_11TH_WASH)
        render_list("Bus — Washington St + 11th St (Stop 20513)", bus11, kind="bus")
except Exception as e:
    with col2:
        st.subheader("Bus — Washington St + 11th St (Stop 20513)")
        st.error(f"MyBus fetch failed: {e}")

try:
    with col3:
        bus10 = parse_njt_mybus(MYBUS_10TH_WASH)
        render_list("Bus — Washington St + 10th St (Stop 20516)", bus10, kind="bus")
except Exception as e:
    with col3:
        st.subheader("Bus — Washington St + 10th St (Stop 20516)")
        st.error(f"MyBus fetch failed: {e}")

# Auto refresh
time.sleep(refresh_sec)
st.rerun()