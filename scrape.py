"""
Scraper for the static "Madrid arrivals" site. Runs in CI on a schedule and
writes data.json — the single file the static front end fetches.

Two data sources:

1. Airport (Madrid Barajas) — aeropuertomadrid-barajas.com. The board exposes the
   whole day via 3-hour time bands, so we get all of it every run and replace.

2. Train station (Madrid Puerta de Atocha, long-distance) — trainoclock.com. That
   board only shows the *next ~15 arrivals*, so a single run can't see the whole
   day. Instead we ACCUMULATE: every run we merge the upcoming trains into today's
   list (deduped by train number) and reset at midnight. Every train passes through
   the "next 15" window before it arrives, so a full day builds up over the day.

Cercanías (commuter) is intentionally not included — no clean live source, and it
would dwarf the taxi-relevant long-distance/airport numbers. Planned as a follow-up
using the scheduled GTFS timetable.

Standard library only.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ---- airport ----
AIRPORT_URL = "https://www.aeropuertomadrid-barajas.com/eng/madrid-airport-flight-arrivals.htm"
TIME_BANDS = ["0-3", "3-6", "6-9", "9-12", "12-15", "15-18", "18-21", "21-0"]
TERMINALS = ["T1", "T2", "T3", "T4"]  # T4S merged into T4

# ---- train ----
ATOCHA_URL = "https://www.trainoclock.com/es-ES/estacion/madridpuertadeatocha/llegadas"

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# airport regexes
_FLIGHT_RE = re.compile(
    r'flightListOtherAirport"><span[^>]*>(\d{2}):\d{2}</span>\s*-\s*(.+?)</div>'
    r'.*?flightListTerminal">([^<]*)<',
    re.S,
)
_FID_RE = re.compile(r"flight-arrival-([A-Z0-9]+)")
_CODE_RE = re.compile(r"^(.*?)\s*\(([A-Z0-9]{3})\)\s*$")
_TAG_RE = re.compile(r"<[^>]+>")
_CLOCK_RE = re.compile(r"(\d{2})/([A-Za-z]{3})/(\d{4})\s+(\d{2}):(\d{2})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# train row: type / number / expected time / origin station
_TRAIN_ROW_RE = re.compile(r"schema\.org/TrainTrip(.*?)(?=schema\.org/TrainTrip|</table|\Z)", re.S)
_T_TYPE_RE = re.compile(r'carrier-line-icon">\s*([A-Za-zÀ-ÿ]+)')
_T_NUM_RE = re.compile(r'tb-train-number[^>]*>\s*([0-9A-Za-z]+)')
_T_TIME_RE = re.compile(r'time-board-time-expected">\s*(\d{2}):\d{2}')
_T_ORIG_RE = re.compile(r'departureStation[^>]*>\s*([^<]+)')


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})
    last = None
    for _ in range(2):  # one retry for transient hiccups
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


# ---------------- airport ----------------
def _norm_terminal(raw):
    raw = (raw or "").strip().upper()
    if raw == "T4S":
        return "T4"
    return raw if raw in TERMINALS else None


def _split_origin(text):
    text = _TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    m = _CODE_RE.match(text)
    return (m.group(1).strip(), m.group(2)) if m else (text, "")


def scrape_airport():
    seen, flights = set(), []
    clock = None  # (hour, "DD Mon, HH:MM", "YYYY-MM-DD")
    for band in TIME_BANDS:
        try:
            html = _fetch(f"{AIRPORT_URL}?t={band}")
        except Exception as e:  # noqa: BLE001
            print(f"  airport band {band}: FAILED ({e})", file=sys.stderr)
            continue
        if clock is None:
            mc = _CLOCK_RE.search(html)
            if mc:
                dd, mon, yr, hh, mm = mc.groups()
                day = f"{yr}-{_MONTHS.get(mon, 0):02d}-{int(dd):02d}"
                clock = (int(hh), f"{dd} {mon}, {hh}:{mm}", day)
        for chunk in re.split(r"flightListRecord", html)[1:]:
            m = _FLIGHT_RE.search(chunk)
            if not m:
                continue
            hour = int(m.group(1))
            fid = _FID_RE.search(chunk)
            key = (fid.group(1) if fid else None, hour, m.group(2))
            if key in seen:
                continue
            seen.add(key)
            term = _norm_terminal(m.group(3))
            if term is None:
                continue
            city, code = _split_origin(m.group(2))
            flights.append({"hour": hour, "terminal": term, "city": city, "code": code})
    flights.sort(key=lambda f: (f["hour"], f["terminal"], f["city"]))
    return flights, clock


# ---------------- train ----------------
def scrape_trains():
    """Return (trains, status_note). The note is surfaced in data.json for debugging
    (e.g. distinguishing an IP block from a parse miss when run from CI)."""
    try:
        html = _fetch(ATOCHA_URL)
    except urllib.error.HTTPError as e:
        return [], f"http_{e.code}"
    except Exception as e:  # noqa: BLE001
        print(f"  atocha: FAILED ({e})", file=sys.stderr)
        return [], f"err_{type(e).__name__}"
    out = []
    for chunk in _TRAIN_ROW_RE.findall(html):
        mt = _T_TIME_RE.search(chunk)
        if not mt:
            continue
        typ = _T_TYPE_RE.search(chunk)
        num = _T_NUM_RE.search(chunk)
        ori = _T_ORIG_RE.search(chunk)
        city = re.sub(r"\s+", " ", ori.group(1)).strip() if ori else ""
        out.append({
            "hour": int(mt.group(1)),
            "type": (typ.group(1).strip() if typ else "Tren"),
            "number": (num.group(1) if num else ""),
            "city": city,
        })
    note = f"ok_{len(out)}" if out else (
        f"parsed0_len{len(html)}_tt{int('TrainTrip' in html)}"
        f"_cf{int('cf-' in html.lower() or 'just a moment' in html.lower())}")
    return out, note


def _merge_trains(base, new):
    """Accumulate today's trains; new entries win (delays update the hour)."""
    d = {}
    for t in base + new:
        k = ("N", t["number"]) if t.get("number") else ("X", t["type"], t["hour"], t["city"])
        d[k] = t
    out = list(d.values())
    out.sort(key=lambda t: (t["hour"], t.get("number", "")))
    return out


def main():
    prev = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:  # noqa: BLE001
            prev = {}
    prev_meta = prev.get("meta", {})

    flights, clock = scrape_airport()
    today = clock[2] if clock else prev_meta.get("day")

    # Airport: full day each run; never clobber good data with an empty scrape.
    if not flights and prev.get("flights"):
        print("Empty airport scrape — keeping previous flights.", file=sys.stderr)
        flights = prev["flights"]

    # Trains: accumulate across runs; reset when the day changes.
    same_day = today is not None and prev_meta.get("day") == today
    base_trains = prev.get("trains", []) if same_day else []
    new_trains, train_note = scrape_trains()
    trains = _merge_trains(base_trains, new_trains)
    if not trains and base_trains:
        trains = base_trains  # atocha fetch failed; keep what we had

    data = {
        "terminals": TERMINALS,
        "flights": flights,
        "trains": trains,
        "meta": {
            "flight_count": len(flights),
            "train_count": len(trains),
            "current_hour": clock[0] if clock else prev_meta.get("current_hour", -1),
            "updated": clock[1] if clock else prev_meta.get("updated", time.strftime("%d %b, %H:%M")),
            "day": today or "",
            "train_status": train_note,
        },
    }

    if not flights and not trains:
        print("Nothing scraped and no history — not writing.", file=sys.stderr)
        return

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT_PATH}: {len(flights)} flights, {len(trains)} trains "
          f"(+{len(new_trains)} train rows this run), day={today}")


if __name__ == "__main__":
    main()
