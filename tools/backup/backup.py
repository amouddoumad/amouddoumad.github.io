"""
Independent backup runner for data.json (HANDOFF.md section 12).

Why: GitHub Actions, which normally runs scrape.py on a schedule, sometimes
loses turns. This is a second runner on someone else's clock: a cron job on
an always-on box the owner controls (setup-vm.sh installs it on any
Debian/Ubuntu host). The logic itself is platform-agnostic and standard
library only. It is a BACKUP, not a second primary:

1. GET data.json from the GitHub Contents API. One call gives us the file
   content (used to seed scrape.py's same-day caches) and its blob sha (needed
   to write back); a second call to the commits API gives the date of the
   commit that last touched the file.
2. If that commit is recent, the GitHub runner is alive — log and return.
   Threshold STALE_MIN (default 35 min). Once we pushed, the last-touching
   commit is our own, so we can't read freshness off it alone; data pushed by
   this runner carries meta.src == "backup" and re-runs on the shorter
   BACKUP_STALE (default 30 min) until the GitHub runner takes over again.
3. Otherwise run the repo's scrape.py (OUT_PATH pointed at a scratch dir),
   stamp meta.src, and PUT it back through the Contents API with a fresh sha.
   On a 409 (the GitHub runner pushed mid-scrape) we re-read the sha and retry
   once; if the file became fresh meanwhile we stand down.

Config comes from environment variables (run_backup.py loads them from
/etc/mad-backup.env on the VM): GH_OWNER, GH_REPO, GITHUB_TOKEN
(fine-grained PAT, Contents: read-write on this repo only), and optionally
STALE_MIN / BACKUP_STALE.
"""

import base64
import datetime
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

API = "https://api.github.com"
UA = "Mozilla/5.0 (compatible; mad-arrivals-backup)"


def _cfg(name, default=None, required=False):
    v = os.environ.get(name) or default
    if required and not v:
        raise RuntimeError(f"missing env {name}")
    return v


def _req(method, url, token=None, body=None):
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def read_current(owner, repo, token=None):
    """Current data.json on the default branch. Returns (data_dict|None, sha,
    committer_datetime_utc). sha is what a write-back must quote. The file and
    its last commit date come from two calls: the Contents API (content + sha)
    and the commits API (the contents response has NO commit object)."""
    url = f"{API}/repos/{owner}/{repo}/contents/data.json"
    try:
        j = _req("GET", url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None, None
        raise
    raw = base64.b64decode(j.get("content", "")) if j.get("content") else b"{}"
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        data = {}
    dt = None
    try:
        cl = _req("GET", f"{API}/repos/{owner}/{repo}/commits?path=data.json&per_page=1", token)
        when = (cl[0].get("commit") or {}).get("committer", {}).get("date") if cl else None
        dt = datetime.datetime.fromisoformat(when.replace("Z", "+00:00")) if when else None
    except Exception:  # noqa: BLE001 — commit age unknown; is_stale() treats None as stale
        pass
    return data, j.get("sha"), dt


def is_stale(data, pushed_at, log=print):
    now = datetime.datetime.now(datetime.timezone.utc)
    if pushed_at is None:
        log("no commit date on data.json — treating as stale")
        return True
    age_min = (now - pushed_at).total_seconds() / 60
    src = (data or {}).get("meta", {}).get("src")
    limit = float(_cfg("BACKUP_STALE", "30")) if src == "backup" else float(_cfg("STALE_MIN", "35"))
    log(f"data.json last touched {int(age_min)} min ago (writer: {src or 'github'}, stale limit {int(limit)} min)")
    return age_min > limit


def run_scrape(prev, tmpdir=None):
    """Run the repo's scrape.py with `prev` (the live data.json) seeded as the
    previous state, and return the freshly scraped dict, or None if the
    scraper refused to write (nothing scraped and no history)."""
    import scrape  # repo root (run_backup.py puts it on sys.path)

    out = os.path.join(tmpdir or tempfile.mkdtemp(prefix="madbak-"), "data.json")
    if prev is not None:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(prev, f, ensure_ascii=False)
    before = hashlib.sha256(open(out, "rb").read()).hexdigest() if os.path.exists(out) else ""
    scrape.OUT_PATH = out  # main() reads/writes the module global
    scrape.main()
    if not os.path.exists(out):
        return None
    raw = open(out, "rb").read()
    if hashlib.sha256(raw).hexdigest() == before:
        return None  # scrape.py left it untouched = it declined to write
    return json.loads(raw)


def push(owner, repo, token, data, sha, log=print):
    meta = data.setdefault("meta", {})
    meta["src"] = "backup"
    meta["pushed_by"] = _cfg("PUSHED_BY", "backup-runner")
    body = {
        "message": "data: backup refresh (independent runner)",
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode(),
        "branch": _cfg("GH_BRANCH", "main"),
    }
    if sha:
        body["sha"] = sha
    url = f"{API}/repos/{owner}/{repo}/contents/data.json"
    try:
        j = _req("PUT", url, token, body)
        log(f"pushed: {j.get('commit', {}).get('sha', '?')[:10]}")
        return True
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        # The GitHub runner committed while we scraped. Re-read; stand down if
        # it brought us fresh data, otherwise retry the write once.
        log(f"409 sha conflict, re-checking: {e.read().decode('utf-8', 'replace')[:120]}")
        fresh, sha2, when = read_current(owner, repo, token)
        if not is_stale(fresh, when, log):
            log("GitHub runner is back — standing down")
            return False
        body["sha"] = sha2
        j = _req("PUT", url, token, body)
        log(f"pushed after conflict: {j.get('commit', {}).get('sha', '?')[:10]}")
        return True


def run_once(log=print, dry=False):
    owner = _cfg("GH_OWNER", "amouddoumad")
    repo = _cfg("GH_REPO", "amouddoumad.github.io")
    token = _cfg("GITHUB_TOKEN", required=not dry)
    data, sha, when = read_current(owner, repo, token)
    if not is_stale(data, when, log):
        return "skipped-fresh"
    if dry:
        log("DRY RUN: would scrape and push now")
        return "would-push"
    fresh = run_scrape(data)
    if fresh is None:
        log("scrape.py wrote nothing — leaving data.json alone")
        return "scrape-empty"
    return "pushed" if push(owner, repo, token, fresh, sha, log) else "stood-down"
