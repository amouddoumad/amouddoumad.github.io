# MAD Arrivals — busiest arrival point now

A **fully static** web app (Spanish UI, built for Madrid taxi drivers) that shows
which arrival point is busiest right now — **Barajas terminals (T1–T4)**, plus
**Atocha** and **Chamartín** train stations — with arrivals per hour. No server
to pay for.

- `index.html` — the whole app (HTML/CSS/JS). Fetches `data.json`, renders in the browser.
- `data.json` — the scraped arrivals data. Refreshed automatically by GitHub Actions.
- `scrape.py` — standard-library Python scraper that writes `data.json`.
- `.github/workflows/update-data.yml` — cron that runs the scraper every ~10 min and commits `data.json`.

## How it works

There is **no backend at runtime**. GitHub Actions runs `scrape.py` on a schedule,
commits the fresh `data.json`, and GitHub Pages serves the static files. Visitors'
browsers only download `index.html` + `data.json` — the airport source is never hit
by visitors, only by the scheduled job (once per run, shared by everyone).

```
GitHub Actions (cron)          GitHub Pages (free static host)      Phone
  every ~10 min                                                     
  python scrape.py  ──commit──►  index.html + data.json  ──fetch──►  renders
```

## One-time hosting setup (free)

1. **Create a public GitHub repo** (public = free Pages + free Actions minutes).
2. **Push these files** to the `main` branch (see commands below).
3. **Allow Actions to commit:** repo **Settings → Actions → General → Workflow
   permissions →** select **“Read and write permissions” → Save.**
   *(Without this, the job can’t push the updated `data.json`.)*
4. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Source:
   “Deploy from a branch” → Branch: `main` / `/ (root)` → Save.**
5. **Run the scraper once now:** repo **Actions → “Update arrivals data” → Run
   workflow.** (After that it runs every ~10 minutes on its own.)

Your site is live at:

```
https://<your-username>.github.io/<your-repo>/
```

**Tip — clean root URL:** name the repo exactly `<org-or-user>.github.io` (e.g.
`amouddoumad.github.io`) to publish at the root `https://amouddoumad.github.io/`
instead of a `/<repo>/` subpath. Only one such site repo is allowed per account/org.
The app uses relative paths, so it works at either location with no changes.

`data.json` is committed already, so the site shows data immediately — the cron just
keeps it fresh.

## Push commands

```bash
cd mad-arrivals
git init -b main
git add .
git commit -m "MAD arrivals static app"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## Local preview

```bash
cd mad-arrivals
python -m http.server 8080
# open http://localhost:8080
```
(Open it through a server, not the `file://` path — browsers block `fetch()` of
`data.json` from `file://`.)

## Notes

- **Freshness:** data is at most ~10 minutes old (the cron interval). GitHub may
  delay scheduled runs a few minutes under load.
- **"Now" is always correct:** the current hour is computed in the browser from
  Europe/Madrid time, so the NOW highlight is right even between scrapes and
  regardless of the viewer's timezone.
- **Resilience:** if a scrape fails (source briefly down), `scrape.py` keeps the
  previous `data.json` instead of publishing zeros.
- **Commit history:** the cron commits `data.json` every 10 min, so history is
  chatty. That's normal and harmless; squash/prune later if you like.
- **Sources:**
  - Airport (live): https://www.aeropuertomadrid-barajas.com/eng/madrid-airport-flight-arrivals.htm
  - Long-distance trains (scheduled): Renfe official AV/LD GTFS — arrivals
    terminating at Atocha (stop `60000`) or Chamartín (stop `17000`).
  - Cercanías (scheduled + real-time delays/cancellations): Renfe national
    Cercanías GTFS + official GTFS-RT trip updates — trains stopping at
    Atocha-Cercanías (`18000`) or Chamartín (`17000`). Shown out of the
    "where to go" ranking by design (commuters rarely take taxis).
- **T4** includes the T4S satellite. **T3** is auxiliary and usually has no arrivals.
