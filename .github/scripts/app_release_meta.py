"""Write latest.json for a freshly built APK (used by .github/workflows/mobile-apk.yml).

    python app_release_meta.py <apk> <version_code> <out.json>

Environment: RELEASE_NOTES (manual notes, or the pushed commit message), FORCE_UPDATE ("true" = older apps must
update before scanning), GIT_SHA, GITHUB_EVENT_NAME. The server (backend/app/routers/mobile_app.py) serves this file
to the installed apps and the public download page.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys


def release_notes(raw: str, event: str) -> str:
    raw = (raw or "").strip()
    if event != "push":
        return raw[:1500]
    # A pushed commit: only its title, without "feat(mobile):" and "(#17)" - short enough for a notification.
    title = raw.splitlines()[0].strip() if raw else ""
    title = re.sub(r"^[a-z]+(\([^)]*\))?!?:\s*", "", title)
    title = re.sub(r"\s*\(#\d+\)\s*$", "", title).strip()
    return title[:1].upper() + title[1:]


def main() -> None:
    apk, code, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with open(apk, "rb") as f:
        data = f.read()
    meta = {
        "version_code": code,
        "version_name": f"1.0.{code - 10000}",  # app/build.gradle: versionCode 10000 + run, versionName 1.0.<run>
        "notes": release_notes(os.environ.get("RELEASE_NOTES", ""), os.environ.get("GITHUB_EVENT_NAME", "")),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "published_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_version_code": code if os.environ.get("FORCE_UPDATE") == "true" else 0,
        "commit": os.environ.get("GIT_SHA", "")[:40],
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
