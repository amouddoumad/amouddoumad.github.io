"""Local exercise for the Azure backup logic — no Azure SDK, no push.

    python tools/azure/test_backup.py          # gate only (fast, network reads)
    python tools/azure/test_backup.py --scrape # also runs the real scrape into a temp dir
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))  # repo root, for scrape.py

import backup  # noqa: E402

if "--scrape" in sys.argv:
    prev, _, _ = backup.read_current("amouddoumad", "amouddoumad.github.io")
    fresh = backup.run_scrape(prev)
    if fresh is None:
        print("scrape wrote nothing")
        sys.exit(1)
    m = fresh["meta"]
    print("scrape OK:", m["flight_count"], "flights,", m["train_count"], "+",
          m["train_ch_count"], "trains,", m["cer_count"], "+", m["cer_ch_count"],
          "cercanias, day", m["day"], "| src would be stamped:", "azure")
else:
    print("gate:", backup.run_once(dry=True))
