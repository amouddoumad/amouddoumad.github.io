#!/usr/bin/env python3
"""Entry point for the backup cron job (the VM) and for local checks.

    run_backup.py            one backup tick (gate → scrape → push)
    run_backup.py --dry      gate only: report the decision, never push
    run_backup.py --no-pull  skip the `git pull` that keeps the repo fresh

Loads KEY=VALUE lines from /etc/mad-backup.env (chmod 600, holds
GITHUB_TOKEN etc.) unless they are already in the environment.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
ENV_FILE = "/etc/mad-backup.env"

sys.path.insert(0, REPO)   # scrape.py
sys.path.insert(0, HERE)   # backup.py

import backup  # noqa: E402


def load_env_file(path=ENV_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def git_pull():
    r = subprocess.run(
        ["git", "-C", REPO, "pull", "--ff-only", "--quiet", "origin", "main"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if r.returncode != 0:
        print(f"git pull failed (continuing with the checkout we have): {r.stderr.strip()[:200]}")


def main(argv):
    dry = "--dry" in argv
    load_env_file()
    if "--no-pull" not in argv and os.path.isdir(os.path.join(REPO, ".git")):
        git_pull()
    result = backup.run_once(dry=dry)
    print("backup tick:", result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
