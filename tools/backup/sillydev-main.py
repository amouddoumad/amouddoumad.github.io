#!/usr/bin/env python3
"""Long-running supervisor for the backup runner on a Silly Development
(Pterodactyl) free server. Panel start command:  python3 sillydev-main.py

Their panel expects ONE persistent process and offers no root or cron, so
this loop plays the role cron plays on dmb5: every TICK seconds (default
300 = 5 min) it
  1. downloads the main-branch tarball from GitHub (stdlib only, no git
     binary needed) and unpacks scrape.py + tools/backup/ into ./run/,
     keeping the checkout layout (run_backup.py locates scrape.py relative
     to itself), so the repo stays the one source of truth;
  2. runs one tick of tools/backup/run_backup.py as a subprocess (a crash
     in a tick never kills the supervisor);
  3. sleeps to the next boundary.

The GitHub token lives in mad-backup.env next to this file — never inside
run/, which gets overwritten by the tarball. It is passed via
MAD_BACKUP_ENV, which run_backup.py reads with top priority. With no token
in place the tick stands down ("no-token") without scraping anything.

Everything this needs that a shared host might not give us is stdlib-only,
which is exactly what scrape.py was always written to be.
"""

import datetime
import io
import os
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run")
ENV = os.environ.get("MAD_BACKUP_ENV", os.path.join(HERE, "mad-backup.env"))
TICK = int(os.environ.get("TICK", "300"))
OWNER = os.environ.get("GH_OWNER", "amouddoumad")
REPO = os.environ.get("GH_REPO", "amouddoumad.github.io")
SHA_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/commits/main?per_page=1"
TAR_URL = f"https://github.com/{OWNER}/{REPO}/archive/refs/heads/main.tar.gz"
WANT = {"scrape.py", "tools/backup/backup.py", "tools/backup/run_backup.py"}
UA = "Mozilla/5.0 (compatible; mad-arrivals-backup)"


def log(msg):
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), msg, flush=True)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        import json as _json
        return _json.load(r)


def live_sha():
    """HEAD sha of main, or None if GitHub is unreachable / rate-limited."""
    try:
        return (_get_json(SHA_URL) or {}).get("sha")
    except Exception:
        return None


def sync():
    """Refresh run/ only when main actually moved. The tarball URL is
    CDN-cached (~5 min), so we fetch it at most once per code change, with
    a cache-buster — cheaper for the free host (no MBs every 5 min) and
    never runs stale code."""
    os.makedirs(RUN, exist_ok=True)
    sha_path = os.path.join(RUN, ".sha")
    cached = open(sha_path).read().strip() if os.path.exists(sha_path) else ""
    need = all(os.path.exists(os.path.join(RUN, w)) for w in WANT)
    sha = live_sha()
    if need and (not sha or sha == cached):
        return
    req = urllib.request.Request(f"{TAR_URL}?cb={sha or int(time.time())}", headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=90).read()  # buffer: tarfile cannot seek an HTTP stream
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as t:
        members = []
        for m in t.getmembers():
            # tar entries are "<repo>-main/<path>"; strip the prefix, keep
            # the rest of the checkout layout intact
            rel = m.name.split("/", 1)[-1] if "/" in m.name else ""
            if m.isfile() and rel in WANT:
                m.name = rel
                members.append(m)
        have = {m.name for m in members}
        if not WANT <= have:
            raise RuntimeError(f"tarball missing {WANT - have}")
        for rel in WANT:
            os.makedirs(os.path.join(RUN, os.path.dirname(rel)), exist_ok=True)
        try:
            t.extractall(RUN, members=members, filter="data")
        except TypeError:  # Python < 3.11.4 has no filter= kwarg
            t.extractall(RUN, members=members)
    fresh = live_sha()  # what we actually just unpacked
    if fresh:
        open(sha_path, "w").write(fresh)
    log(f"synced run/ at {(fresh or sha or '?')[:10]}")


def tick():
    env = {**os.environ, "MAD_BACKUP_ENV": ENV, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run([sys.executable, "tools/backup/run_backup.py"], cwd=RUN,
                       env=env, timeout=900)
    return r.returncode


def main():
    log(f"supervisor up; tick every {TICK}s; env file {ENV}")
    while True:
        start = time.time()
        try:
            sync()
            code = tick()
            log(f"tick exit {code}")
        except Exception as e:
            log(f"tick failed: {type(e).__name__}: {e}")
        time.sleep(max(5, TICK - (time.time() - start)))


if __name__ == "__main__":
    main()
