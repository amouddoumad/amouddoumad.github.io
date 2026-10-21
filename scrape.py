"""
Scraper for the static "Madrid arrivals" site. Runs in CI on a schedule and
writes data.json — the single file the static front end fetches.

Data sources:

1. Airport (Madrid Barajas) — aeropuertomadrid-barajas.com. Live board, whole day
   via 3-hour time bands. Replaced every run. If that page comes back EMPTY (Aena
   answers 403 to some hosts — see tools/aena_probe.py, 2026-10-13), the fallback
   is Aena's own JSON endpoint on www.aena.es, which served 200 from the same
   blocked IP. It carries the whole day (and both neighbours) in one call; city
   names are Spanish there. meta.flight_status records which source was used.

   TOMORROW comes from Aena's JSON endpoint on EVERY run (flights_next, meta.next_day)
   — the HTML page only publishes a 6-hour tomorrow tail (bands "0-3+1", "3-6+1").
   Its rows are one per flight NUMBER (codeshare variants), so we dedup on
   (exact scheduled minute, terminal, city), which collapses codeshares and keeps
   distinct flights (night check 2026-10-21: 17-23h window = 222 flights by minute
   key, exactly the live HTML board's 222 rows; the old hour key merged ~5-15%).

2. Train stations, long-distance (Madrid-Puerta de Atocha stop 60000 and
   Madrid-Chamartín stop 17000) — Renfe's official AV/LD GTFS timetable
   (ssl.renfe.com). We count SCHEDULED arrivals (trains TERMINATING at each
   station) per hour for today's service calendar. This is a timetable, not a live
   board — trainoclock's live board is behind Cloudflare and returns 403 to
   datacenter/CI IPs, so GTFS is the reliable source that also gives the full day
   at once. Recomputed once per day and cached in data.json between runs.

3. Cercanías (Atocha stop 18000 and Chamartín stop 17000) — Renfe's national
   Cercanías GTFS (Fichero_CER_FOMENTO; the Fichero_CERCANIAS zip does NOT contain
   Madrid). Every train STOPPING at the station counts (through stations —
   passengers alight from through trains too, unlike LD where we count terminating
   trips). Schedules are cached once per day like LD. On top of them, Renfe's
   official GTFS-RT trip updates (gtfsrt.renfe.com, refreshed every 20 s, trip_ids
   match the GTFS) give live delays/cancellations — re-fetched on EVERY run into
   the small `cer_rt`/`cer_rt_ch` maps (the delay is read at each station's own
   stopId); the front end applies them to the cached schedules.

Standard library only.
"""

import csv
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

# ---- airport ----
AIRPORT_URL = "https://www.aeropuertomadrid-barajas.com/eng/madrid-airport-flight-arrivals.htm"
TIME_BANDS = ["0-3", "3-6", "6-9", "9-12", "12-15", "15-18", "18-21", "21-0"]
TERMINALS = ["T1", "T2", "T3", "T4"]  # T4S merged into T4
# Aena's own JSON board (public, keyless). Different host from the HTML page, so
# it keeps answering when the HTML site 403s a host (aena_probe.py evidence,
# 2026-10-13). dosDias=si returns today + tomorrow; tipoVuelo L = arrivals.
AENA_JSON_URL = ("https://www.aena.es/sites/Satellite"
                 "?pagename=AENA_ConsultarVuelos&airport=MAD&flightType=L&dosDias=si")
try:  # stdlib since 3.9; the fixed offset is a last-resort fallback only
    from zoneinfo import ZoneInfo
    _MAD_TZ = ZoneInfo("Europe/Madrid")
except Exception:  # noqa: BLE001 - Windows without the tzdata package
    _MAD_TZ = datetime.timezone(datetime.timedelta(hours=1))

# ---- trains (Renfe AV/LD GTFS) ----
GTFS_URL = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"
ATOCHA_STOP = "60000"  # Madrid-Puerta de Atocha-Almudena Grandes (long-distance)
CHAM_STOP = "17000"    # Madrid-Chamartín-Clara Campoamor (same id in the Cercanías feed)

# ---- cercanías (Renfe national Cercanías/Rodalies GTFS + GTFS-RT) ----
CER_GTFS_URL = "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip"
CER_STOP = "18000"  # Madrid-Atocha Cercanías
CER_RT_URL = "https://gtfsrt.renfe.com/trip_updates.json"

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

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
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


def _fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
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
            html = _fetch_text(f"{AIRPORT_URL}?t={band}")
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


def scrape_airport_json(dates):
    """Aena's public JSON board: physical arrivals for each date in `dates`
    (YYYY-MM-DD). The HTML page (scrape_airport) gives one row per flight; this
    feed has one row per flight NUMBER, so codeshare variants appear separately —
    we dedup on (exact scheduled minute, terminal, city), which collapses a
    codeshare (same schedule instant) while keeping distinct flights that merely
    share an hour. Two uses: fetch tomorrow in full on EVERY run (the HTML page
    only publishes a 6-h tomorrow tail), and serve as today's fallback when the
    HTML host returns empty. City names are Spanish here. Returns
    ({iso_date: [flights]}, clock)."""
    try:
        rows = json.loads(_fetch_bytes(AENA_JSON_URL).decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        print(f"  airport JSON: FAILED ({e})", file=sys.stderr)
        return {}, None
    if not isinstance(rows, list):
        return {}, None
    now = datetime.datetime.now(_MAD_TZ)
    keys = {d: datetime.date.fromisoformat(d).strftime("%d/%m/%Y") for d in dates}  # `fecha` format
    by_date = {d: [] for d in dates}
    seen = {d: set() for d in dates}
    for r in rows:
        if not isinstance(r, dict) or r.get("tipoVuelo") != "L":
            continue
        iso = next((i for i, k in keys.items() if r.get("fecha") == k), None)
        if iso is None:
            continue
        if str(r.get("estado") or "").upper() in ("CANC", "CANCELLED"):
            continue
        term = _norm_terminal(r.get("terminal"))
        if term is None:
            continue
        hp = str(r.get("horaProgramada") or "")
        if not re.match(r"^\d{2}:", hp):
            continue
        city = str(r.get("ciudadIataOtro") or "").strip()
        code = str(r.get("iataOtro") or "").strip()
        if not city:
            continue
        k = (hp[:5], term, city)  # exact scheduled minute — codeshares share it
        if k in seen[iso]:
            continue
        seen[iso].add(k)
        by_date[iso].append({"hour": int(hp[:2]), "terminal": term,
                             "city": city.title(), "code": code})
    for lst in by_date.values():
        lst.sort(key=lambda f: (f["hour"], f["terminal"], f["city"]))
    clock = (now.hour, now.strftime("%d %b, %H:%M"), now.strftime("%Y-%m-%d"))
    return by_date, clock


# ---------------- train (GTFS) ----------------
def _gtfs_rows(z, name):
    """Yield dict rows from a GTFS csv, stripping header + value whitespace
    (Renfe's files pad the last column with spaces)."""
    f = io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace")
    reader = csv.reader(f)
    hdr = [h.strip() for h in next(reader)]
    for row in reader:
        yield {h: (v.strip() if isinstance(v, str) else v) for h, v in zip(hdr, row)}


def _active_services(z, date_str, weekday):
    wd = _WEEKDAYS[weekday]
    active = set()
    for r in _gtfs_rows(z, "calendar.txt"):
        if r.get(wd) == "1" and r["start_date"] <= date_str <= r["end_date"]:
            active.add(r["service_id"])
    if "calendar_dates.txt" in z.namelist():  # the Cercanías feed omits it
        for r in _gtfs_rows(z, "calendar_dates.txt"):
            if r.get("date") == date_str:
                if r.get("exception_type") == "1":
                    active.add(r["service_id"])
                elif r.get("exception_type") == "2":
                    active.discard(r["service_id"])
    return active


def scrape_trains(day_iso):
    """Scheduled long-distance arrivals terminating at Atocha (60000) or Chamartín
    (17000) for `day_iso` (YYYY-MM-DD) — one parse serves both stations. Called
    for today AND tomorrow on cache-miss days. Returns
    ({stop_id: [trains]}, {stop_id: note})."""
    wanted = (ATOCHA_STOP, CHAM_STOP)
    if not day_iso:
        day_iso = datetime.date.today().isoformat()
    try:
        raw = _fetch_bytes(GTFS_URL)
    except urllib.error.HTTPError as e:
        return {s: [] for s in wanted}, {s: f"http_{e.code}" for s in wanted}
    except Exception as e:  # noqa: BLE001
        return {s: [] for s in wanted}, {s: f"err_{type(e).__name__}" for s in wanted}
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        d = datetime.date.fromisoformat(day_iso)
        date_str, weekday = d.strftime("%Y%m%d"), d.weekday()
        active = _active_services(z, date_str, weekday)

        routes = {}
        for r in _gtfs_rows(z, "routes.txt"):
            routes[r["route_id"]] = (r.get("route_short_name") or r.get("route_desc") or "Tren").strip() or "Tren"
        trips = {}
        for t in _gtfs_rows(z, "trips.txt"):
            if t["service_id"] in active:
                trips[t["trip_id"]] = {"route": t["route_id"], "num": (t.get("trip_short_name") or "").strip()}
        stops = {s["stop_id"]: s["stop_name"] for s in _gtfs_rows(z, "stops.txt")}

        # single pass: per (active) trip track final stop (max seq) and origin (min seq)
        final, origin = {}, {}
        for st in _gtfs_rows(z, "stop_times.txt"):
            tid = st["trip_id"]
            if tid not in trips:
                continue
            seq = int(st["stop_sequence"])
            fx = final.get(tid)
            if fx is None or seq > fx[0]:
                final[tid] = (seq, st["stop_id"], st["arrival_time"])
            ox = origin.get(tid)
            if ox is None or seq < ox[0]:
                origin[tid] = (seq, st["stop_id"])

        out = {s: [] for s in wanted}
        for tid, (seq, sid, at) in final.items():
            if sid not in out:  # arrivals = trips terminating at our stations
                continue
            try:
                hour = int(at.split(":")[0]) % 24
            except Exception:  # noqa: BLE001
                continue
            typ = routes.get(trips[tid]["route"], "Tren")
            city = stops.get(origin.get(tid, (0, ""))[1], "")
            out[sid].append({"hour": hour, "type": typ, "number": trips[tid]["num"], "city": city})
        notes = {}
        for s, lst in out.items():
            lst.sort(key=lambda t: (t["hour"], t.get("number", "")))
            notes[s] = f"ok_{len(lst)}"
        return out, notes
    except Exception as e:  # noqa: BLE001
        return {s: [] for s in wanted}, {s: f"parse_{type(e).__name__}" for s in wanted}


# ---------------- cercanías (GTFS schedule + GTFS-RT live) ----------------
def scrape_cercanias(today):
    """Scheduled Cercanías trains STOPPING at Madrid-Atocha (18000) or
    Madrid-Chamartín (17000) for `today` — one parse serves both stations. Returns
    ({stop_id: [items]}, {stop_id: note}). Items are compact — {"h": hour,
    "m": minute, "l": line (C1..C10), "t": trip_id} — `t` is what joins the
    GTFS-RT feed."""
    wanted = (CER_STOP, CHAM_STOP)
    if not today:
        today = datetime.date.today().isoformat()
    try:
        raw = _fetch_bytes(CER_GTFS_URL)
    except urllib.error.HTTPError as e:
        return {s: [] for s in wanted}, {s: f"http_{e.code}" for s in wanted}
    except Exception as e:  # noqa: BLE001
        return {s: [] for s in wanted}, {s: f"err_{type(e).__name__}" for s in wanted}
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        d = datetime.date.fromisoformat(today)
        active = _active_services(z, d.strftime("%Y%m%d"), d.weekday())

        routes = {}
        for r in _gtfs_rows(z, "routes.txt"):
            routes[r["route_id"]] = (r.get("route_short_name") or "C?").strip().upper() or "C?"
        line = {}  # active trip_id -> line name
        for t in _gtfs_rows(z, "trips.txt"):
            if t["service_id"] in active:
                line[t["trip_id"]] = routes.get(t["route_id"], "C?")

        out = {s: [] for s in wanted}
        for st in _gtfs_rows(z, "stop_times.txt"):
            sid = st["stop_id"]
            if sid not in out:
                continue
            tid = st["trip_id"]
            if tid not in line:
                continue
            try:
                hh, mm = st["arrival_time"].split(":")[:2]
                out[sid].append({"h": int(hh) % 24, "m": int(mm), "l": line[tid], "t": tid})
            except Exception:  # noqa: BLE001
                continue
        notes = {}
        for s, lst in out.items():
            lst.sort(key=lambda c: (c["h"], c["m"]))
            notes[s] = f"ok_{len(lst)}"
        return out, notes
    except Exception as e:  # noqa: BLE001
        return {s: [] for s in wanted}, {s: f"parse_{type(e).__name__}" for s in wanted}


def cer_realtime(items_by_stop):
    """Live delays/cancellations for the cached Cercanías schedules, from Renfe's
    official GTFS-RT trip updates — one feed fetch serves all stations, but the
    delay is read at EACH station's own stopId (a train can be on time at Atocha
    yet late reaching Chamartín). Returns ({stop_id: {trip_id: minutes | "X"}},
    note) — "X" = cancelled (or station skipped); only delays >= 1 min are
    recorded so the maps stay small. Missing trips simply run on time."""
    ids_by_stop = {s: {it["t"] for it in lst} for s, lst in items_by_stop.items() if lst}
    if not ids_by_stop:
        return {}, "skip"
    try:
        feed = json.loads(_fetch_text(CER_RT_URL))
    except Exception as e:  # noqa: BLE001
        return {s: {} for s in ids_by_stop}, f"err_{type(e).__name__}"
    out = {s: {} for s in ids_by_stop}
    for ent in feed.get("entity", []):
        tu = ent.get("tripUpdate") or {}
        tid = (tu.get("trip") or {}).get("tripId")
        if tid is None:
            continue
        canceled = (tu.get("trip") or {}).get("scheduleRelationship") == "CANCELED"
        stu_by_stop = {stu.get("stopId"): stu for stu in (tu.get("stopTimeUpdate") or [])}
        for sid, ids in ids_by_stop.items():
            if tid not in ids:
                continue
            stu = stu_by_stop.get(sid)
            if canceled or (stu is not None and stu.get("scheduleRelationship") == "SKIPPED"):
                out[sid][tid] = "X"
                continue
            delay = tu.get("delay")  # trip-level fallback
            if stu is not None:
                a = (stu.get("arrival") or {}).get("delay")
                if isinstance(a, (int, float)):
                    delay = a
            if isinstance(delay, (int, float)) and abs(delay) >= 60:
                out[sid][tid] = round(delay / 60)
    return out, f"ok_{sum(len(m) for m in out.values())}"


def main():
    prev = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:  # noqa: BLE001
            prev = {}
    prev_meta = prev.get("meta", {})

    now_mad = datetime.datetime.now(_MAD_TZ)

    def _valid_date(s):
        try:
            datetime.date.fromisoformat(s)
            return s
        except Exception:  # noqa: BLE001
            return None

    flights, clock = scrape_airport()
    flight_note = "html" if flights else None
    today = (_valid_date(clock[2] if clock else None) or _valid_date(prev_meta.get("day"))
             or now_mad.strftime("%Y-%m-%d"))
    tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()

    json_by = None
    if not flights:  # e.g. the HTML host 403s us (2026-10-13) — Aena's JSON also serves today
        json_by, jclock = scrape_airport_json([today, tomorrow])
        flights = json_by.get(today, [])
        flight_note = "json" if flights else None
        clock = clock or jclock
    else:
        json_by, _ = scrape_airport_json([tomorrow])  # tomorrow full-day (HTML gives only a 6-h tail)
    flights_next = (json_by or {}).get(tomorrow, [])
    flight_next_note = "json" if flights_next else None

    if not flights and prev.get("flights"):
        print("Empty airport scrape — keeping previous flights.", file=sys.stderr)
        flights = prev["flights"]
        flight_note = "kept-previous"
    if not flights_next and prev.get("flights_next") and prev_meta.get("next_day") == tomorrow:
        print("Empty tomorrow airport scrape — keeping previous flights_next.", file=sys.stderr)
        flights_next = prev["flights_next"]
        flight_next_note = "kept-previous"

    # Trains: the schedule doesn't change during the day, so compute today AND
    # tomorrow once per day and reuse the cached results on later runs. Same
    # policy for Cercanías below (today only — fixed timetable, out of the
    # ranking; the Mañana view skips the strip to keep data.json small).
    # Both stations are parsed together, so the cache is used only when every
    # list is cached-and-ok — a missing one (e.g. the first run after adding a
    # station or the tomorrow day) forces a fresh parse.
    same_day = prev_meta.get("day") == today
    same_tom = prev_meta.get("next_day") == tomorrow
    trains = trains_ch = trains_next = trains_ch_next = []
    train_note = train_ch_note = train_next_note = train_ch_next_note = None
    if (same_day and same_tom and prev.get("trains") and prev.get("trains_ch")
            and prev.get("trains_next") and prev.get("trains_ch_next")
            and all(str(prev_meta.get(k, "")).startswith("ok") for k in
                    ("train_status", "train_ch_status", "train_next_status", "train_ch_next_status"))):
        trains, train_note = prev["trains"], prev_meta.get("train_status")
        trains_ch, train_ch_note = prev["trains_ch"], prev_meta.get("train_ch_status")
        trains_next, train_next_note = prev["trains_next"], prev_meta.get("train_next_status")
        trains_ch_next, train_ch_next_note = prev["trains_ch_next"], prev_meta.get("train_ch_next_status")
    else:
        for d, cache_ok in ((today, same_day), (tomorrow, same_tom)):
            by_stop, notes = scrape_trains(d)
            ato, ato_note = by_stop[ATOCHA_STOP], notes[ATOCHA_STOP]
            cha, cha_note = by_stop[CHAM_STOP], notes[CHAM_STOP]
            if d == today:  # keep last good per station on a failed refresh
                if not ato and cache_ok and prev.get("trains"):
                    ato, ato_note = prev["trains"], prev_meta.get("train_status")
                if not cha and cache_ok and prev.get("trains_ch"):
                    cha, cha_note = prev["trains_ch"], prev_meta.get("train_ch_status")
                trains, train_note, trains_ch, train_ch_note = ato, ato_note, cha, cha_note
            else:
                if not ato and cache_ok and prev.get("trains_next"):
                    ato, ato_note = prev["trains_next"], prev_meta.get("train_next_status")
                if not cha and cache_ok and prev.get("trains_ch_next"):
                    cha, cha_note = prev["trains_ch_next"], prev_meta.get("train_ch_next_status")
                trains_next, train_next_note, trains_ch_next, train_ch_next_note = \
                    ato, ato_note, cha, cha_note

    cercanias, cercanias_ch = [], []
    cer_note = cer_ch_note = None
    if (same_day and prev.get("cercanias") and prev.get("cercanias_ch")
            and str(prev_meta.get("cer_status", "")).startswith("ok")
            and str(prev_meta.get("cer_ch_status", "")).startswith("ok")):
        cercanias, cer_note = prev["cercanias"], prev_meta.get("cer_status")
        cercanias_ch, cer_ch_note = prev["cercanias_ch"], prev_meta.get("cer_ch_status")
    else:
        by_stop, notes = scrape_cercanias(today)
        cercanias, cer_note = by_stop[CER_STOP], notes[CER_STOP]
        cercanias_ch, cer_ch_note = by_stop[CHAM_STOP], notes[CHAM_STOP]
        if not cercanias and same_day and prev.get("cercanias"):
            cercanias, cer_note = prev["cercanias"], prev_meta.get("cer_status")
        if not cercanias_ch and same_day and prev.get("cercanias_ch"):
            cercanias_ch, cer_ch_note = prev["cercanias_ch"], prev_meta.get("cer_ch_status")

    # Live layer refreshes on EVERY run (schedules above are the daily-cached base).
    cer_rt_by, cer_rt_note = cer_realtime({CER_STOP: cercanias, CHAM_STOP: cercanias_ch})
    cer_rt = cer_rt_by.get(CER_STOP, {})
    cer_rt_ch = cer_rt_by.get(CHAM_STOP, {})

    data = {
        "terminals": TERMINALS,
        "flights": flights,
        "flights_next": flights_next,
        "trains": trains,
        "trains_ch": trains_ch,
        "trains_next": trains_next,
        "trains_ch_next": trains_ch_next,
        "cercanias": cercanias,
        "cercanias_ch": cercanias_ch,
        "cer_rt": cer_rt,
        "cer_rt_ch": cer_rt_ch,
        "meta": {
            "flight_count": len(flights),
            "flight_status": flight_note or "empty",
            "train_count": len(trains),
            "train_ch_count": len(trains_ch),
            "cer_count": len(cercanias),
            "cer_ch_count": len(cercanias_ch),
            "current_hour": clock[0] if clock else prev_meta.get("current_hour", -1),
            "updated": clock[1] if clock else prev_meta.get("updated", time.strftime("%d %b, %H:%M")),
            "day": today or "",
            "next_day": tomorrow or "",
            "flight_next_count": len(flights_next),
            "flight_next_status": flight_next_note or "empty",
            "train_next_count": len(trains_next),
            "train_ch_next_count": len(trains_ch_next),
            "train_next_status": train_next_note,
            "train_ch_next_status": train_ch_next_note,
            "train_status": train_note,
            "train_ch_status": train_ch_note,
            "cer_status": cer_note,
            "cer_ch_status": cer_ch_note,
            "cer_rt_status": cer_rt_note,
        },
    }

    if not flights and not trains and not trains_ch:
        print("Nothing scraped and no history — not writing.", file=sys.stderr)
        return

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT_PATH}: {len(flights)} flights + {len(flights_next)} tomorrow "
          f"({flight_note}/{flight_next_note}), {len(trains)}+{len(trains_ch)} trains "
          f"({train_note}/{train_ch_note}), {len(trains_next)}+{len(trains_ch_next)} tomorrow "
          f"({train_next_note}/{train_ch_next_note}), {len(cercanias)}+{len(cercanias_ch)} cercanías "
          f"({cer_note}/{cer_ch_note}, rt {cer_rt_note}), day={today} next={tomorrow}")


if __name__ == "__main__":
    main()
