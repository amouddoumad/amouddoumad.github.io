#!/bin/sh
# One-shot bootstrap for the backup runner (run as root, idempotent).
# Any always-on Debian/Ubuntu box will do (apt-get is the only distro hook)
# — see HANDOFF.md §12.
# After running: put the GitHub PAT into /etc/mad-backup.env, then check with
#   cd /opt/mad-arrivals && python3 tools/backup/run_backup.py --dry
set -eu

REPO_DIR=/opt/mad-arrivals
URL=https://github.com/amouddoumad/amouddoumad.github.io.git

command -v git >/dev/null || apt-get install -y -qq git
command -v python3 >/dev/null || apt-get install -y -qq python3

if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull --ff-only --quiet origin main
else
    git clone --quiet "$URL" "$REPO_DIR"
fi

if [ ! -f /etc/mad-backup.env ]; then
    cat > /etc/mad-backup.env <<'EOF'
GITHUB_TOKEN=PASTE-FINE-GRAINED-PAT-HERE
GH_OWNER=amouddoumad
GH_REPO=amouddoumad.github.io
# STALE_MIN=35
# BACKUP_STALE=30
# PUSHED_BY=backup-runner
EOF
    chmod 600 /etc/mad-backup.env
    echo ">> EDIT /etc/mad-backup.env and set GITHUB_TOKEN (fine-grained PAT, this repo, Contents: RW)."
fi

# -n: a hung run never stacks up; the pull inside keeps scrape.py current.
cat > /etc/cron.d/mad-backup <<'EOF'
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/10 * * * * root flock -n /run/lock/mad-backup python3 /opt/mad-arrivals/tools/backup/run_backup.py >> /var/log/mad-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/mad-backup

# Try a tick end to end right now (pushes only if GitHub looks dead; a missing
# PAT shows up as an error here, which is fine — cron will start once it is set).
cd "$REPO_DIR" && python3 tools/backup/run_backup.py || echo ">> first run reported a problem (PAT set yet?)"
echo ">> setup done; cron every 10 min, log: /var/log/mad-backup.log"
