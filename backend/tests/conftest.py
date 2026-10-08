import os
import sys
import tempfile
from pathlib import Path

# Configure an isolated mock environment BEFORE the app is imported.
_tmp = Path(tempfile.mkdtemp(prefix="fwdscan_test_"))
os.environ["DATABASE_URL"] = "sqlite:///" + (_tmp / "test.db").as_posix()
os.environ["OMSGURU_USE_MOCK"] = "true"
os.environ["SYNC_ENABLED"] = "false"
os.environ["BACKUP_ENABLED"] = "false"  # no scheduler in tests; the backup functions are tested directly
os.environ["BACKUP_DIR"] = (_tmp / "backups").as_posix()
os.environ["BACKUP_OFFSITE_REMOTE"] = ""  # never the developer's real offsite copy ...
os.environ["RCLONE_CONFIG"] = (_tmp / "no-rclone.conf").as_posix()  # ... nor its "gdrive" sign-in
os.environ["APP_SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"
os.environ["ADMIN_EMAIL"] = ""  # the .env default admin is not created in tests (see test_users.py)
os.environ["ADMIN_NAME"] = "Administrator"
os.environ["BATCH_LIMIT"] = "100"
os.environ["CACHE_MIN_INTERVAL"] = "0"  # tests read numbers right after each change
os.environ["APP_RELEASE_DIR"] = (_tmp / "app").as_posix()  # where the APK workflow drops the app
os.environ["PUBLIC_URL"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
