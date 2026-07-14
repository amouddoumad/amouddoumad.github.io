# HANDOFF — Madrid Llegadas (airport + Atocha)

Handoff notes for the next agent/model working on this project. Read this fully
before changing anything — several non-obvious constraints will bite you otherwise.

---

## 1. What this is

A **fully static** mobile web app that tells a **Madrid taxi driver, in Spanish,
which arrival point is busiest right now so they can go there.** It covers:

- **Barajas airport** arrivals per terminal (T1–T4), live.
- **Atocha (Puerta de Atocha) long-distance trains** per hour, scheduled.

The signature screen is a unified **"¿Dónde ir ahora?"** hero: airport terminals
and Atocha ranked together as bars, with the top one flagged **"VE AQUÍ"**.

- **Live site:** https://amouddoumad.github.io/
- **Repo:** `amouddoumad/amouddoumad.github.io` (GitHub Pages, org site, public)
- **Local clone:** `C:\Users\BoualiN\mad-arrivals`

### Product goals / constraints (do not regress these)
- **Spanish UI**, **extreme mobile-friendliness**, **light + dark mode** (toggle, remembered, defaults to system).
- **Free hosting only** — no paid infra, no server that costs money.
- **"NOW" must always be correct** regardless of data staleness → the current hour
  is computed **in the browser** from `Europe/Madrid` time, never from the data.
- Origins are **hidden by default** (tap a row to reveal). Top-origins panels are collapsed by default.

---

## 2. Architecture (how it works)

There is **no backend at runtime.** GitHub Actions runs `scrape.py` on a schedule,
commits `data.json`, and GitHub Pages serves the static files. The visitor's browser
only downloads `index.html` + `data.json`.

```
GitHub Actions (cron ~10 min)          GitHub Pages (free static host)      Phone
  python scrape.py  ──commit data.json──►  index.html + data.json  ──fetch──►  renders
```

**Why not scrape in the browser?** CORS. The data sources don't send
`Access-Control-Allow-Origin`, so a browser `fetch()` to them is blocked. Scraping
*must* happen server-side (in CI). This is the core constraint that shaped everything.

### File map
| File | Role |
|---|---|
| `index.html` | The entire app: HTML + CSS + vanilla JS. Fetches `data.json`, renders. No build step, no framework, no external assets. |
| `scrape.py` | Standard-library-only scraper. Writes `data.json`. Runs in CI. |
| `data.json` | Scraped data. **Owned by the cron — do not hand-edit or hand-commit** (see §6). |
| `.github/workflows/update-data.yml` | Cron (`*/10`), `workflow_dispatch`, and push-on-`scrape.py`. Commits `data.json`. |
| `README.md` | User-facing setup/hosting notes. |
| `HANDOFF.md` | This file. |

---

## 3. Data sources & why they were chosen

### Airport — LIVE, full day ✅
- URL: `https://www.aeropuertomadrid-barajas.com/eng/madrid-airport-flight-arrivals.htm?t=<band>`
- Bands: `0-3,3-6,6-9,9-12,12-15,15-18,18-21,21-0` → whole day in one run.
- Parse: split on `flightListRecord`; regex pulls time, origin (`flightListOtherAirport`,
  may be wrapped in `<a>` → strip tags), terminal (`flightListTerminal`). **T4S → T4.**
  Dedupe by `(flight-arrival id, hour, origin)`.
- Also parses the page's Madrid-local clock (`DD/Mon/YYYY HH:MM`) → `meta.current_hour`,
  `meta.updated`, `meta.day` (YYYY-MM-DD, used as the authoritative "today").

### Trains (Atocha long-distance) — SCHEDULED, full day ✅
- Source: **Renfe official AV/LD GTFS** — `https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip`
  (plain nginx, ~745 KB, **not IP-blocked**).
- **Atocha stop_id = `60000`** (Madrid-Puerta de Atocha-Almudena Grandes).
- "Arrivals" = trips whose **final stop** (max `stop_sequence`) is `60000`. Arrival hour
  from `arrival_time` (`% 24` for after-midnight). Origin = the trip's **first stop** name.
  Type = `route_short_name` (AVE / AVANT / ALVIA / AVLO / Intercity …).
- Filtered to **today's active services** via `calendar.txt` + `calendar_dates.txt`.
- ~110–120 arrivals/day. **This is a timetable, not real-time** (no live delays/cancellations).

### Rejected sources (don't waste time re-trying these)
- **trainoclock.com** (live train board): clean HTML **but behind Cloudflare → returns
  `HTTP 403` to GitHub's datacenter IP.** Works from a residential IP, not from CI.
  Richer headers won't reliably bypass Cloudflare IP-reputation blocking. This is *why*
  we use scheduled GTFS instead of a live board. (`meta.train_status` records this: e.g.
  `http_403`, `ok_116`, `parse_*`.)
- **Adif official** (`adif.es`): 403 to scrapers; live board is app/JS only.
- **mytrainpal / trip.com**: arrivals come from a private client-side API, not in the HTML.
- **Renfe undocumented real-time API** (`flotaLD.json`): exists, fragile, undocumented,
  gives in-transit GPS snapshots not a station timetable. Not used.

---

## 4. `data.json` schema

```json
{
  "terminals": ["T1","T2","T3","T4"],
  "flights":  [{"hour":9,"terminal":"T4","city":"Barcelona","code":"BCN"}, ...],
  "trains":   [{"hour":9,"type":"AVE","number":"02061","city":"Sevilla-Santa Justa"}, ...],
  "meta": {
    "flight_count": 570,
    "train_count": 116,
    "current_hour": 15,          // Madrid hour at scrape time (client recomputes live)
    "updated": "14 Jul, 15:28",  // Madrid local; client localizes month → "14 jul"
    "day": "2026-07-14",         // authoritative "today" (from airport clock)
    "train_status": "ok_116"     // diagnostic: ok_N | http_403 | err_* | parse_*
  }
}
```
The front end tolerates missing `trains`/fields (renders empty). Keep it backward-compatible.

---

## 5. How to update the live site

**All edits happen locally, then push. Pages redeploys automatically on any push to `main`.**

### Editing the UI (`index.html`)
1. Edit `index.html`.
2. `git add index.html && git commit -m "..."`
3. **`git pull --rebase origin main`** ← REQUIRED (see §6), then `git push origin main`.
4. Pages rebuilds (~1 min). Editing only `index.html` does **not** re-run the scraper
   (fine — data is unchanged).

### Editing the scraper (`scrape.py`)
Same as above. The workflow's `push` trigger includes `paths: scrape.py`, so **pushing
`scrape.py` auto-runs the scraper** and regenerates `data.json` within a couple minutes.

### Force a data refresh manually
GitHub → **Actions → "Update arrivals data" → Run workflow** (this is `workflow_dispatch`).

### Repo settings that must stay on (already configured)
- **Settings → Actions → General → Workflow permissions = Read and write** (so cron can commit).
- **Settings → Pages → Deploy from a branch → `main` / `(root)`.**

---

## 6. GIT GOTCHAS — read before pushing (this is where things break)

1. **The cron commits `data.json` every ~10 min**, so your local `main` is almost always
   behind the remote. **Always `git pull --rebase origin main` before `git push`.** Your
   code commits (which don't touch `data.json`) rebase cleanly on top of the bot's commits.
2. **Do NOT stage/commit/delete `data.json` yourself.** It's cron-owned. If you touched it
   locally (e.g. testing), run `git checkout -- data.json` before committing. Committing it
   causes rebase conflicts with the bot; deleting it breaks the site.
3. **Environment:** Windows, Git Bash available. No `gh` CLI. Git identity is `Nacirbl`;
   credential helper is `manager` (system) — pushes to the org repo work non-interactively
   as long as that account keeps push rights. Use `GIT_TERMINAL_PROMPT=0` to fail fast
   instead of hanging if creds are missing.
4. **Line endings:** the repo stores **LF** (verified). The workflow YAML/`scrape.py` run on
   Linux CI — never let them get committed as CRLF (would break the shell `run:` block).
   The local "LF will be replaced by CRLF" warning is harmless (working-copy only).

---

## 7. Verifying changes

### Local preview (must use a server, not `file://` — CORS blocks `fetch` of data.json)
```bash
cd C:/Users/BoualiN/mad-arrivals
python -m http.server 8080        # → http://localhost:8080   (port 5000 is taken on this machine)
python scrape.py                  # regenerate data.json locally (downloads GTFS ~12s)
```

### Screenshot check (headless Edge)
```bash
"/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new --disable-gpu \
  --no-sandbox --hide-scrollbars --force-device-scale-factor=2 --window-size=500,1250 \
  --virtual-time-budget=6000 --screenshot="OUT.png" "http://127.0.0.1:8080/"
```
- **GOTCHA:** Chromium on Windows has a **~500 px minimum window width.** Requesting
  `--window-size=390` renders a 500 px layout cropped to 390 → *false* horizontal overflow.
  **Always verify at ≥ 500 px.** The real page is responsive down to ~360 px on phones.
- For dark/light force: add `--blink-settings=preferredColorScheme=0` (dark) or `=1` (light).
  Do NOT use `--enable-features=WebContentsForceDark` — it re-inverts pixels and lies to you.

### Verify the LIVE site (public APIs, no auth)
```bash
# live data + train diagnostic
curl -s "https://amouddoumad.github.io/data.json?_=$RANDOM" | python -m json.tool | head
# recent workflow runs
curl -s "https://api.github.com/repos/amouddoumad/amouddoumad.github.io/actions/runs?per_page=5"
```
Check `meta.train_status` — `ok_N` is healthy; `http_403`/`err_*`/`parse_*` means the train
source failed that run.

---

## 8. Other constraints / facts worth knowing

- **GitHub cron is best-effort**, not exact: `*/10` realistically fires every ~10–60 min and
  can be throttled/skipped (documented GitHub behavior). The app tolerates this because NOW
  is client-side. If reliable freshness is needed, use an **external scheduler** (cron-job.org
  / UptimeRobot) hitting the `workflow_dispatch` API with a token stored *in that service*.
- **Encoding:** all sources are UTF-8. A Windows terminal may render accents as `�` — the
  actual bytes/data are fine. `data.json` is written `ensure_ascii=False`, UTF-8.
- **Failure resilience:** a failed airport scrape keeps the previous flights; a failed train
  refresh keeps the previous trains — an empty scrape never clobbers good data with zeros.
- **Temp files:** use the session scratchpad, not the repo, for screenshots/experiments.

### ⚠️ Train cache gotcha (important if you change train logic)
`scrape.py` computes the train schedule **once per day and caches it** in `data.json`
(`cached_ok` reuses `prev['trains']` when `meta.day` is unchanged and `train_status`
starts with `ok`). **Consequence:** if you change the train-parsing code and push mid-day,
the running job will *reuse the cached old trains* and your change won't show until the next
day. To force a same-day re-parse: temporarily bypass the cache (e.g. make `cached_ok=False`),
or commit is not enough — the simplest is to trigger a run after `meta.day` rolls over, or
edit the cache check. Remember to restore the cache logic afterward.

---

## 9. Improvement backlog (highest value first)

1. **Cercanías (commuter) at Atocha — the explicitly-requested next step.**
   - The user wants Cercanías shown **separated from long-distance AND their sum.**
   - Source: a **Cercanías Madrid GTFS** (the AV/LD feed does NOT contain Cercanías trains;
     stop `18000` = "Madrid-Atocha Cercanías" appears in stops.txt but has no LD trips).
     Find the correct Renfe/NAP Cercanías GTFS (the guessed `Fichero_Cercanias_Madrid` path
     404'd; look on `data.renfe.com` / `nap.mitma.es`).
   - **Volume caveat:** Cercanías is dozens of trains/hour — it will **dwarf** airport
     terminals and LD. **Keep it OUT of the "¿Dónde ir ahora?" ranking** (Cercanías riders
     rarely take taxis) or normalize; show it as a separate info series. This was a
     deliberate product decision.
   - Data model: extend `trains` items with a `service` field (`"LD"` | `"CER"`), or add a
     separate `cercanias` array. The Atocha table can then become `LD | Cercanías | Σ`.

2. **Live trains (optional):** current trains are *scheduled*. If real-time is wanted, the
   only realistic path is a **Cloudflare Worker** (or similar) that proxies a live board —
   but note trainoclock is itself on Cloudflare, so test whether a Worker is allowed; or use
   Renfe's fragile `flotaLD.json`. Weigh against the reliability we have now.

3. **Reliable freshness:** add an external `workflow_dispatch` pinger (see §8) if the
   ~10-min cadence isn't holding.

4. **City-name localization (optional):** airport origins are English ("London", "Rome");
   could map to Spanish ("Londres", "Roma"). Train origins are already Spanish (Renfe data).

5. **Docs:** `README.md` still describes the airport-only version — update it to mention
   trains/GTFS and this handoff.

---

## 10. TL;DR for the impatient
- Static site + GitHub cron. Edit `index.html`/`scrape.py` locally → `git pull --rebase` →
  `git push`. Never touch `data.json`. Verify at ≥500px and via the live `data.json`.
- Airport = live board (full day). Atocha = Renfe GTFS schedule (full day, not real-time)
  because the live board is Cloudflare-blocked from CI.
- Next task: **Cercanías via its own GTFS**, shown separately + summed, kept out of the
  "go here" ranking.
