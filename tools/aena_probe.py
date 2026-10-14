#!/usr/bin/env python3
"""One-shot reachability probe for the two Aena endpoints, used to diagnose
an IP block (e.g. the 2026-10-13 sillydev case). Writes both verdicts and
exits; designed to be set as the container startup file for one restart.

Control = the HTML arrivals page scrape.py normally reads.
Subject = Aena's public keyless JSON endpoint on a different host (aena.es).

    python3 tools/aena_probe.py    # writes ./aena_test.txt (or the path below)
"""
import datetime
import os
import urllib.error
import urllib.request

CONTROL = ("https://www.aeropuertomadrid-barajas.com/eng/"
           "madrid-airport-flight-arrivals.htm")
SUBJECT = ("https://www.aena.es/sites/Satellite"
           "?pagename=AENA_ConsultarVuelos&airport=MAD&flightType=L&dosDias=si")
OUT = os.environ.get("AENA_PROBE_OUT", "/home/container/aena_test.txt")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read()
        return f"OK status={r.status} bytes={len(body)}"
    except urllib.error.HTTPError as e:
        return f"FAIL HTTPError {e.code}"
    except Exception as e:  # noqa: BLE001 - report anything, this is a probe
        return f"FAIL {type(e).__name__}: {e}"


def main():
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %H:%M:%SZ")
    lines = [
        f"{now}",
        f"control(aeropuertomadrid-barajas.com): {fetch(CONTROL)}",
        f"subject(www.aena.es JSON): {fetch(SUBJECT)}",
    ]
    text = "\n".join(lines) + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
