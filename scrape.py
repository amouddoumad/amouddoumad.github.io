"""
Standalone scraper for the static MAD-arrivals site.

Runs in CI (GitHub Actions) on a schedule. Fetches the Madrid Barajas arrivals
feed across all eight 3-hour time bands, dedupes, and writes data.json — the
single file the static front end fetches. No web server, no dependencies beyond
the Python standard library.

Safety: if a run scrapes nothing (e.g. the source is briefly down), it keeps the
existing data.json instead of clobbering good data with an empty file.
"""

import json
import os
import re
import sys
import time
import urllib.request

SOURCE_URL = "https://www.aeropuertomadrid-barajas.com/eng/madrid-airport-flight-arrivals.htm"
TIME_BANDS = ["0-3", "3-6", "6-9", "9-12", "12-15", "15-18", "18-21", "21-0"]
TERMINALS = ["T1", "T2", "T3", "T4"]  # T4S merged into T4
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

_RECORD_RE = re.compile(
    r'flightListOtherAirport"><span[^>]*>(\d{2}):\d{2}</span>\s*-\s*(.+?)</div>'
    r'.*?flightListTerminal">([^<]*)<',
    re.S,
)
_ID_RE = re.compile(r"flight-arrival-([A-Z0-9]+)")
_CODE_RE = re.compile(r"^(.*?)\s*\(([A-Z0-9]{3})\)\s*$")
_TAG_RE = re.compile(r"<[^>]+>")
_CLOCK_RE = re.compile(r"(\d{2})/([A-Za-z]{3})/(\d{4})\s+(\d{2}):(\d{2})")


def _fetch_band(band):
    req = urllib.request.Request(
        f"{SOURCE_URL}?t={band}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    last_err = None
    for _ in range(2):  # one retry to ride out transient network hiccups
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    raise last_err


def _normalise_terminal(raw):
    raw = (raw or "").strip().upper()
    if raw == "T4S":
        return "T4"
    return raw if raw in TERMINALS else None


def _split_origin(text):
    text = _TAG_RE.sub("", text)  # some origins are wrapped in <a> links
    text = re.sub(r"\s+", " ", text).strip()
    m = _CODE_RE.match(text)
    if m:
        return m.group(1).strip(), m.group(2)
    return text, ""


def scrape():
    seen = set()
    flights = []
    clock = None  # (hour, "DD Mon, HH:MM") parsed from the page's Madrid-local time
    ok_bands = 0

    for band in TIME_BANDS:
        try:
            html = _fetch_band(band)
        except Exception as e:  # noqa: BLE001
            print(f"  band {band}: FAILED ({e})", file=sys.stderr)
            continue
        ok_bands += 1
        if clock is None:
            mc = _CLOCK_RE.search(html)
            if mc:
                clock = (int(mc.group(4)), f"{mc.group(1)} {mc.group(2)}, {mc.group(4)}:{mc.group(5)}")
        for chunk in re.split(r"flightListRecord", html)[1:]:
            m = _RECORD_RE.search(chunk)
            if not m:
                continue
            hour = int(m.group(1))
            fid = _ID_RE.search(chunk)
            key = (fid.group(1) if fid else None, hour, m.group(2))
            if key in seen:
                continue
            seen.add(key)
            term = _normalise_terminal(m.group(3))
            if term is None:
                continue
            city, code = _split_origin(m.group(2))
            flights.append({"hour": hour, "terminal": term, "city": city, "code": code})

    flights.sort(key=lambda f: (f["hour"], f["terminal"], f["city"]))
    meta = {
        "count": len(flights),
        "current_hour": clock[0] if clock else -1,
        "updated": clock[1] if clock else time.strftime("%d %b, %H:%M"),
        "ok_bands": ok_bands,
    }
    return {"terminals": TERMINALS, "flights": flights, "meta": meta}


def main():
    data = scrape()
    n = len(data["flights"])
    print(f"Scraped {n} flights from {data['meta']['ok_bands']}/{len(TIME_BANDS)} bands.")

    if n == 0 and os.path.exists(OUT_PATH):
        # Every band failed. Do not overwrite the last good data with zeros.
        print("Empty scrape — keeping existing data.json.", file=sys.stderr)
        return

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
