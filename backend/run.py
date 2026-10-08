"""Start the Forward Scan server.

    python run.py              # live: talks to OMSGuru with the credentials in ../.env
    python run.py --demo       # demo: sample orders, separate demo database, no OMSGuru calls

Other PCs / handheld scanners on the same network open http://<this-pc-ip>:8000
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEMO_ADMIN_PASSWORD = "demo-admin-2026"  # demo database only; the live admin password lives in .env


def main() -> None:
    p = argparse.ArgumentParser(description="Forward Scan server")
    p.add_argument("--demo", action="store_true", help="use mock OMSGuru data and a separate demo database")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = p.parse_args()

    if args.demo:
        # Must be set before the app (and its .env loading) is imported; real env vars win over .env.
        os.environ["OMSGURU_USE_MOCK"] = "true"
        os.environ["DATABASE_URL"] = "sqlite:///demo_forward_scan.db"
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = DEMO_ADMIN_PASSWORD
        os.environ["ADMIN_EMAIL"] = ""  # the demo signs in as admin, not as the live default admin
        os.environ.setdefault("SYNC_INTERVAL_SECONDS", "30")
        os.environ["BACKUP_ENABLED"] = "false"  # nothing worth keeping in the demo database

    import uvicorn

    # One worker on purpose: the OMS sync loop and live-update hub run in-process.
    # Client IPs (sign-in throttle, logs) come from X-Forwarded-For only when the proxy in front is trusted:
    # FORWARDED_ALLOW_IPS="*" in the container, which only Traefik can reach (port bound to 127.0.0.1).
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1, proxy_headers=True,
                forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"), log_level="info")


if __name__ == "__main__":
    main()
