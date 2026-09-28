"""Build the deployment zip for the Function App. The zip root must hold
function_app.py / host.json / requirements.txt plus the code they import, and
scrape.py comes from the repo root (one source of truth, no copied fork).

    python tools/azure/make_package.py        ->  tools/azure/azfunc.zip

Deploy (publish profile credentials from the portal, Get publish profile):
    curl -u <user>:<pass> --data-binary @tools/azure/azfunc.zip ^
         https://<app-name>.scm.azurewebsites.net/api/publish
"""

import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(HERE, "azfunc.zip")

FILES = [
    (HERE, "function_app.py"),
    (HERE, "host.json"),
    (HERE, "requirements.txt"),
    (HERE, "backup.py"),
    (ROOT, "scrape.py"),
]

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for base, name in FILES:
        z.write(os.path.join(base, name), name)
print(OUT, os.path.getsize(OUT), "bytes")
