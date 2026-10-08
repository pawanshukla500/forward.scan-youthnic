# Forward Scan - OMSGuru dispatch scanning

Multi-user web app for forward (outbound) dispatch scanning, marketplace by marketplace
(OMSGuru "Sales Channel"). Built for 4,500+ shipments a day across many scan stations.

## What it does

| On every scan | |
|---|---|
| **Instant order lookup** | AWB resolves against a local copy of OMSGuru orders - shows order id, invoice, courier, COD/prepaid, amount, buyer, SKUs, OMS status |
| **Duplicate block** | An AWB can be dispatched once, ever. Second scan = blue screen + 3 quick beeps, with who/when/which channel scanned it first. Safe across simultaneous stations (DB unique key) |
| **Marketplace segregation** | Station picks a sales channel; an AWB from another channel is rejected ("Wrong marketplace - belongs to Flipkart") |
| **Cancelled / return block** | Orders cancelled in OMSGuru are rejected; if an order is cancelled *after* it was scanned, the scan gets a red alert |
| **Not found** | AWB not in OMS yet -> accepted as purple "not found / unverified", triggers an immediate sync and turns green automatically when the order appears |
| **Pause on error** | After a red scan the scanner pauses until someone presses Enter, so the bad packet gets set aside |

Every result has its own colour and its own sound (no voice), so a packer knows it without reading the screen:

| Result | Colour | Sound |
|---|---|---|
| OK - verified | Green | 1 short beep |
| Duplicate - already scanned | Blue | 3 quick beeps |
| Not found in OMSGuru yet (saved as unverified) | Purple | 2 tones, high then low |
| Saved, but check (not packed in OMS, partly cancelled, ...) | Amber | 2 beeps |
| Stop - wrong marketplace, cancelled, return, invalid | Red | buzzer |

The legend under the scan box plays each sound when tapped.

Plus: live dashboard per marketplace (scanned / pending / duplicates / wrong-bag / blocked, hourly + 14-day charts,
per-user counts), date-wise reports with Excel export, pending list (Ready-to-ship in OMS but not scanned, sorted by SLA),
users with roles (scanner / supervisor / manager / admin).

## Two things OMSGuru's API cannot do (and how the app handles them)

1. **No "mark as dispatched" endpoint.** The public API (v1.0.1) has no call to update an order to Shipped, so
   dispatch is still marked in OMSGuru itself. The app only reads OMSGuru. (The Manifests screen - courier hand-over
   sheet and an OMSGuru dispatch CSV - was removed from the menu on 3 Oct 2026 as not needed; its server code in
   `backend/app/routers/manifests.py` is still there if it is ever wanted back.)
2. **No lookup by AWB, and only 60 calls / 5 minutes.** The app keeps its own copy of recent AWBs (see below).
   Scans never wait for the API. The limit is **shared by every integration using your client id** - watch
   *Admin -> OMSGuru sync*.

## What is synced and kept

| Data | Synced? | Kept |
|---|---|---|
| Orders **Packed** or **Ready to ship** in your dispatch warehouse | Yes - full refresh every 30 min, new AWBs every minute, live on every scan | While Packed / Ready-to-ship (they are pending) |
| Every AWB generated in OMSGuru (= invoice date) | Once, when it appears | `RETAIN_ORDERS_DAYS` (7 days), per sales channel |
| An order that **leaves** Packed / Ready-to-ship (shipped, in transit, cancelled, returned) | No longer synced - last known status kept | Until its AWB is 7 days old |
| Orders **cancelled before an AWB existed** | Never | Never stored |
| **Scans** (per sales channel, with a copy of the order details) | - | `SCAN_RETENTION_DAYS` from the scan date (default 1095 = 3 years; 0 = forever) |

On first start the app loads today's AWBs and all Ready-to-ship orders straight away, then fills the previous
6 days in the background using only spare API credits. The clean-up runs every 15 minutes and needs no API calls.

## Pending & reconciliation

An AWB generated today must be dispatched today. *Pending & reconciliation* shows, per sales channel and per AWB
date (last 7 days):

| Bucket | Meaning |
|---|---|
| **Scanned** | Forward-scanned by the team |
| **Pending** | Not scanned, still Ready-to-ship in OMS - must go out |
| **Overdue** | Pending, and the AWB was generated on an earlier day |
| **Left RTS, not scanned** | OMS no longer shows it Ready-to-ship (shipped / in transit) but it was never forward-scanned |
| **Cancelled after AWB** | Cancelled or returned after the label was made - excluded from pending, blocked if scanned |

Every bucket opens the AWB list (with SLA, age, courier, SKUs) and exports to Excel. The scan station shows the
same numbers for its channel, updating live with every scan. *Scans & reports -> Channel summary* gives the
long-term, channel-wise scan history by day or month.

**Count orders from (Admin -> OMSGuru sync).** An admin sets the day the team started working in this app. Only
AWBs generated (orders packed / made ready to ship) on or after that day count as generated, pending, overdue or
reconciled; the OMSGuru history fill does not reach back before it. Older orders still open in OMSGuru can be
scanned, they are just not counted. The Pending page shows the date in use.

**Start fresh** (empty orders, scans and sync history; keep users, channels and warehouses): close the server, then
`.venv\Scripts\python backend\clear_data.py --yes --start 2026-10-03`. A checked copy of the database is saved to
`backups\before-clear_<time>\` first. On the next start the open orders download from OMSGuru again.

### How you know the numbers are right

| Check | When | What it does |
|---|---|---|
| **Cross-check with OMSGuru** | After every Packed + Ready-to-ship refresh (2 API calls) | Compares OMSGuru's own pending report (`order_aging`) with the local copy, per sales channel, for orders of the last `ORDER_LOOKBACK_DAYS`. A gap of more than 2 orders (or 1 %) triggers one early re-refresh; if it is still there, *Admin* turns red. The Pending page shows "match OMSGuru - checked HH:MM". |
| **Exit check** | Spare API credits only | An order that leaves Ready-to-ship **without a scan** is looked up once in OMSGuru, so *Left RTS, not scanned* holds real misses (shipped / in transit) and late cancellations move to *Cancelled after AWB*. |
| **Refresh order** | Every refresh | Packed is read before Ready-to-ship (the direction orders move), so an order marked RTS while the refresh runs is never wrongly counted as "left". |

Packed / Ready-to-ship orders older than `ORDER_LOOKBACK_DAYS` (15) are not tracked. On 2 Oct 2026 that was 37 Shopify
orders stuck in Packed for 21-32 days, none with an AWB - the cross-check lists them separately.

## How every scan fetches the order from OMSGuru

Verified against the live API (2 Oct 2026): OMSGuru has **no way to search by AWB** - `order_details` accepts only
`order_id`, `sub_order_id` or `id` (AWB-style params return "Invalid Params provided"; an AWB passed as order id returns
"No order found"; list endpoints ignore AWB filters). So each scan does this:

| Step | API calls | What happens |
|---|---|---|
| 1. Duplicate check | 0 | Answered from the database - duplicates never spend a credit |
| 2. AWB -> order | 0 | Local index built from invoices (every invoice carries `shipment_tracker` = AWB) |
| 3. **Live fetch** | 1 | `order_details` by sub-order id or order id, whichever works for that channel - fresh status, items, buyer, courier (~0.35 s) |
| 3b. AWB not in index | 1-3 | Pull invoices created since the last sync; if still unknown, try the barcode as order id / sub-order id |
| 4. Validate + save | 0 | Duplicate / wrong marketplace / cancelled / return, using the fresh data |

OMSGuru answers `order_details` differently per channel (tested on the live API, 2 Oct 2026):

| Channel | By sub-order id | By order id |
|---|---|---|
| Myntra PPMP, Myntra Youthnic, Cocoblu | the exact shipment | works |
| Flipkart, Meesho | ~50 **unrelated** rows (no match) | ONE shipment of the order |
| Ajio | "No order found" | works |

The app learns this per channel: it tries what last worked, drops a method that never found anything for that channel,
and only trusts rows with the same AWB or sub-order id (*Admin -> OMSGuru sync* shows the method per channel). One gap
remains: a Flipkart order split into several shipments (~1 %) - the order id returns only one of them, possibly the
wrong one, so those scans are judged on the local copy (refreshed every `OPEN_ORDERS_REFRESH_MINUTES`) and the screen
says "unconfirmed". Scanning an order-id barcode picks the shipment that is still open; if two are open it asks for the AWB.

Budget: OMSGuru allows 12 calls/minute, shared with every integration on the account. Scan-time calls jump the
queue and never wait - when no credit is free (or OMSGuru takes longer than `LIVE_TIMEOUT_SECONDS`) the scan is checked
against the local copy and the screen says so. Background sync leaves `LIVE_HEADROOM` credits free for scans. At more
than ~10 scans/minute sustained, the extra scans use the local copy (synced every `SYNC_INTERVAL_SECONDS`).
*Admin -> OMSGuru sync* shows how many scans were served live.

## Run it

Requirements: Python 3.11+ and Node 18+ installed. Then just double-click **`run.bat`**. The first run creates the
Python environment, installs packages and builds the web app; after that it starts straight away and shows a menu:

```
1. Start LIVE    - real OMSGuru orders              (http://localhost:8000)
2. Start DEMO    - sample orders, nothing sent      (http://localhost:8001)
3. Rebuild web app  - after code changes
4. Run tests
```

It prints the addresses other PCs/phones should open and opens the browser for you. Shortcuts from a terminal:
`run.bat live`, `run.bat demo`, `run.bat live 9000` (other port), `run.bat rebuild`, `run.bat test`.

- **Live:** sign in with the default admin's email (`ADMIN_EMAIL`) or username (`ADMIN_USERNAME`) and `ADMIN_PASSWORD`
  from `.env`. That account is created at startup when no user has that email, and never overwritten afterwards -
  change the password in the app (key icon next to your name, bottom left).
- **Demo:** separate `demo_forward_scan.db`; login `admin` with the password in `backend/run.py` (`DEMO_ADMIN_PASSWORD`).

### Users, roles and passwords

All accounts live in the database (`users` table); passwords are stored only as bcrypt hashes.

| Role | Can |
|---|---|
| **Scan operator** | Scan, see their station, undo their own scan for 10 minutes |
| **Supervisor** / **Manager** | + dashboards, reports and exports, remove scans, see the team; **scanner accounts only**: add scanner IDs, reset a scanner's password, disable / enable a scanner |
| **Admin** | + every account and role (create, change roles, names, emails, reset, disable), all settings |

- *Admin -> Overview -> Team members -> Create user*: name, optional email, username, role and a password (**Generate**
  makes a readable one). Managers and supervisors see **Add scanner** instead: the new login is always a Scan operator. People sign in with their email or username. Leave *"choose their own password the first time
  they sign in"* ticked: until they do, nothing else opens.
- **Reset password** (key icon on the row): sets a new password, signs the person out everywhere and asks them to
  choose their own at the next sign-in. **Edit** changes name / email; the person icon disables or re-enables sign-in.
- Everyone can change their own password with the key icon next to their name (bottom left). Other devices signed in
  with that account are signed out.
- Passwords need at least 8 characters with a letter and a number, and may not be the username or email. 8 wrong
  attempts in 10 minutes pause sign-in for that name for a few minutes. An admin cannot demote or disable themselves.

Other PCs, tablets and Android handhelds on the same network open `http://<server-pc-ip>:8000`
(allow Python through Windows Firewall when asked). USB/Bluetooth barcode scanners work as keyboards - configure them
to send **Enter** after each barcode.

First live start: channels and warehouses load from OMSGuru automatically. Then in **Admin**:
1. *Sales channels* - switch scanning on/off per channel; if the yellow "not mapped" box appears, paste the shown label
   into the right channel's "Also known as".
2. *Warehouses* - make sure your dispatch warehouse is listed with Sync on.
3. *Team members* (Admin overview) - create a login per packer (role Scan operator) and for leads (Supervisor or
   Manager); leads can then add scanner IDs for new packers themselves.

## Android app (APK)

Actions -> **Build Android APK** -> Run workflow builds the app against `https://scan.youthnic.shop` and publishes
`forward-scan-app.apk` on the `forward-scan-app` release (untick *publish* for a build-only check). On the phone the
camera stays open in the top half of the scan screen and every result appears below it; the version (`1.0.<run>`) is
shown in Android's App info.

Every build is signed with one fixed key from the `ANDROID_DEBUG_KEYSTORE_B64` repository secret, so a new APK installs
over the old one and the phone stays signed in. The key file is backed up (not in git) at
`backups/android-signing/forward-scan-debug.keystore` (alias `androiddebugkey`, password `android`); if the secret is ever
lost, restore it with `base64 -w0 <that file> | gh secret set ANDROID_DEBUG_KEYSTORE_B64`. A build with a different key
cannot update installed apps - phones would have to uninstall first.

## Daily flow

1. Packer signs in -> **Scan** -> picks the marketplace bag (e.g. *VB EXPORT - Myntra PPMP*) -> scans.
2. Supervisor watches **Dashboard**; clears **Pending** before pickup.
3. After pickup, *Pending -> Left RTS, not scanned* lists anything OMSGuru shows shipped that was never scanned.

## Backups & restore

The server backs itself up while stations keep scanning, with SQLite (this PC) and PostgreSQL (production):

| Copy | When | Kept | Where |
|---|---|---|---|
| **Full backup** - SQLite: copied through SQLite, `quick_check`, gzip; PostgreSQL: `pg_dump` (custom format), read back with `pg_restore --list` | Daily after `BACKUP_FULL_HOUR` (02:00), and at first start | 14 daily + 12 monthly | `daily\`, `monthly\` |
| **Recent scans** (scans + audit of the last 2 days, users, channels, manifests; a small SQLite file in both cases) | Every 15 min when something was scanned | 96 copies (24 h) | `recent\` |
| **Offsite copy** (Google Drive, see below) | Every full backup, recent scans at most hourly; size + md5 read back | 60 days daily, 12 months monthly, 3 days recent | `Forward Scan Backups/<folder>/` in Drive |

Folders: SQLite `backups\<database name>\` next to the database; PostgreSQL `data/backups/pg-<database name>/`
(`/opt/forward-scan/Forward-Scan/data/...` on the server, kept across deploys).

*Admin -> Overview* shows "Database & backups": **Protected** (all copies fine), **Only on this server / PC** (amber: no
second copy yet), **At risk** (red: a backup or an upload failed, or is too old). *Admin -> OMSGuru sync* has the
details and **Back up now**.

### Offsite copy (Google Drive)

Without a copy somewhere else, a lost server or disk loses every backup with it. The server uploads to Google Drive
with [rclone](https://rclone.org) (in the Docker image) using its own sign-in in `data/rclone/rclone.conf` (kept
across deploys, not in git). It only gets access to the files it creates itself (`drive.file` scope). Set up once:

1. On the server: `docker run --rm --network host --entrypoint rclone forward-scan:latest authorize "drive" "eyJzY29wZSI6ImRyaXZlLmZpbGUifQ"`
   (prints a link; from a PC, open an SSH tunnel first: `ssh -L 53682:127.0.0.1:53682 root@<server>`).
2. Open the link in a browser on that PC, sign in with the Google account that should hold the backups, allow.
3. Paste the printed token into `rclone config create gdrive drive scope=drive.file token='<token>'` run with
   `RCLONE_CONFIG=/app/data/rclone/rclone.conf` inside the app container (`docker exec -it Forward-Scan ...`).
4. *Admin -> OMSGuru sync -> Back up now*: the card shows "Uploaded ... min ago" and the overview turns **Protected**.

A different target (S3, Backblaze B2, another Drive folder): any rclone remote in that file plus
`BACKUP_OFFSITE_REMOTE=<remote>:<path>` (GitHub variable for the server). `BACKUP_MIRROR_DIR` (a NAS / USB path)
still works as well. Also keep a copy of `.env` / the GitHub secrets somewhere safe.

### Restore

Orders re-sync from OMSGuru by themselves; scans come from the backups.

**PostgreSQL (production server):**

```
cd /opt/forward-scan/Forward-Scan
docker stop Forward-Scan
docker compose -p forward-scan -f docker-compose.production.yml run --rm --no-deps forward-scan python backend/restore_backup.py
docker compose -p forward-scan -f docker-compose.production.yml run --rm --no-deps forward-scan python backend/restore_backup.py --latest --yes
docker start Forward-Scan
```

The first `run` lists the backups. With `--latest --yes` it saves the current database to
`data/backups/incident-<time>/` (`pg_dump`), restores the newest full backup (`pg_restore --clean` in one transaction)
and merges the newest recent-scans copy taken after it. A backup from Google Drive: put the `fs-pg-*.dump` (and its
`.json`) into `data/backups/pg-<database>/daily/`, or pass its path instead of `--latest`.

**SQLite (this PC):**

1. Close the server window.
2. From the project folder: `.venv\Scripts\python backend\restore_backup.py` lists the backups;
   `.venv\Scripts\python backend\restore_backup.py --latest --yes` restores the newest full backup plus the newest
   recent-scans copy taken after it. The current database files (including `-wal` / `-shm`) are moved to
   `backups\incident-<time>\` first - never put a restored file next to an old `-wal`.
3. Start the server (`run.bat`).

Packets scanned after the recovery point can simply be scanned again (duplicates are still blocked).

Scans under a year of `SCAN_RETENTION_DAYS` need `SCAN_RETENTION_FORCE=true` (a typo would otherwise delete years of
history at the next start). Old scans are deleted 2,000 at a time so stations never wait on the clean-up.

## Capacity & database

Load-tested on this PC (i5-12450H, 3 Oct 2026) with simulated stations that scan a packet every 2-4 s and refresh
their screen like the real scan page, on a database holding a year of scans (1.64 million):

| Stations | Scans / min | p50 / p99 per scan | Errors | Server CPU |
|---|---|---|---|---|
| 10 | 200 | 39 ms / 175 ms | 0 | ~60 % of one core |
| 25 | 500 | 50 ms / 161 ms | 0 | ~61 % of one core |
| 25, OMSGuru simulated (0.35 s calls, 60 per 5 min) | 500 | 45 ms / 1.0 s | 0 | ~51 % |

Before the 3 Oct fixes the same server froze for 30-60 s at 10-15 stations. Estimated ceiling: ~800 scans/min in
one process. At more than ~10 scans/min most scans are checked against the local copy (OMSGuru allows ~12 calls a
minute, shared); the background sync always keeps credits for new AWBs.

What keeps it fast: one database connection per worker (pool of 50 + 20), no database work on the event loop,
each scan returns its connection while OMSGuru answers, numbers that every screen re-reads after a scan are
computed once per second per channel and shared, composite indexes matched to the real queries (the operators
report went from 5.4 s to 0.05 s on a year of data), batched clean-up, `synchronous=FULL` (no committed scan is lost
on a power cut; commit p99 ~7 ms).

Storage: about 2.9 GB per year of scans (half of it the per-scan copy of the order details). Known slow spots that
are not used during scanning: the scans list over a whole year (first load can take 20+ s) and `/api/lookup`.

One server process on purpose - the sync loop and live updates run in-process. To host it centrally, set
`DATABASE_URL` to Postgres (`pip install "psycopg[binary]"`) and deploy `backend/run.py` with the built
`frontend/dist`; first fix the Postgres gaps found in the review (column length limits, `ensure_columns` defaults,
sequence reset after copying data).

## Tests

```bash
cd backend && ..\.venv\Scripts\python -m pytest -q
```
