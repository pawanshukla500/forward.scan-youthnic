# Forward Scan - design decisions

**Source of truth since 2026-10-02: the Figma Make design "Forward Scan Scanning Page"** (green ForwardScan
brand, DM Sans, soft 14 px cards, eyebrow labels). MASTER.md (ui-ux-pro-max) still holds the general rules
(contrast, touch targets, motion, live-data labelling). This file lists where the build deliberately differs
from the Figma file and why. Tokens live in `frontend/src/index.css`; shared parts in
`frontend/src/components/ui.tsx` and `components/Shell.tsx`.

| Area | Figma file | We use | Why |
|---|---|---|---|
| Type sizes | 8-11 px labels/body | >= 12 px everywhere, 15-16 px body | Read at arm's length on the packing table; WCAG resize text |
| Muted text | `#6e7b74` | `#5f6b65` light / `#96a49c` dark | Figma value was 4.1:1 on the page; needs 4.5:1 |
| Control borders | `#e2e8e4` | `#7a8a80` light / `#64736a` dark | Inputs and buttons need 3:1 against the surface (WCAG 1.4.11) |
| Dark accent | `#126b4e` text on dark | `#5ac69a` with near-black text on it | Brand green is 2.1:1 on the dark surface |
| Data | Sample numbers | Every number from the API (`/api/scan-context`, `/api/dashboard`, `/api/marketplaces`, `/api/reports/*`, `/api/admin/sync`) | No fake data in an operations tool |
| Scan result | Small "Shipment loaded" pill | Full-width status band, one colour + one sound per result: green OK / blue duplicate / purple not found / amber check / red stop (no voice, 3 Oct 2026) | Packers must read the verdict from a metre away without reading text |
| Page order on Scan | Lookup → marketplace progress → shipment | One compact lookup panel (marketplace + scan box + scanner state, then today's numbers and AWB progress in one row) → **shipment** → queue (5 Oct 2026) | On a 1366x768 laptop the verdict, AWB, order details and items of the scanned packet must all be on screen without scrolling |
| Shipment card | Card head with AWB, separate multi-item box, actions at the bottom | AWB inside the verdict band; tags + Flag / Undo / Next in one bar; details and items side by side when the card is >= 680 px wide; multi-item warning is the amber items heading; journey beside the card only from 1360 px (a strip below it otherwise) | Same information in about half the height |
| "Complete scan" | Saves on button press | Saved on Enter (scanner); button is "Next shipment" | 4,500+ scans/day: a scanner gun cannot press a button |
| Fragile / weight / phone | Shown | Not shown | OMSGuru does not provide them; we never invent fields |
| Google SSO, "trusted by" proof | Shown on login | Removed | Not available / not true; "Forgot password" tells the user to ask an admin |
| Carrier "Serviceable" | Pill | OMS units / status | No serviceability API |
| Admin "webhooks", "scanner devices" | Cards | Live lookup, database, channel and warehouse counts | Only show systems that exist |
| Fonts | DM Sans via Google | Bundled via `@fontsource` (DM Sans + Fira Code for AWBs) | Works when the warehouse internet drops |

Contrast was checked for every text pair (>= 4.5:1) and control border (>= 3:1) in both themes.

## Page rules
- **Shell**: the header carries the page's only `h1` (title + subtitle per route); pages start with a toolbar.
  F2 jumps to Forward Scan (last marketplace used). Help card hides on short screens; a `?` button in the
  header opens the same scanner guide. Bell = real alerts after scan, stopped scans, flags and overdue AWBs.
- **Scan** (`pages/scan-station.md`): one atomic `aria-live` announcement per scan; pause-on-error holds the
  scanner after a buzzer until Enter; multi-item shipments get the amber items heading; phones get
  Camera / Manual tabs (camera needs https:// or localhost and Chrome's BarcodeDetector, otherwise Manual).
- **Dashboard** (`pages/dashboard.md`): metric cards link to the list behind them; week-on-week compares with
  the same time of day last week; status colour always has an icon + label.
- **Reports**: one spreadsheet card; bottom sheet tabs switch views; Excel/CSV come from the server for scans,
  pending and channel summary, CSV from the loaded rows for SKU / operator / audit views.
- Loading uses skeletons that reserve the final layout; toasts auto-dismiss (3.5 s ok / 6 s error).
- Custom CSS lives in `@layer base` / `@layer components` so Tailwind utilities always win over it.
- Theme: system by default, sun/moon toggle per device (stations in bright areas usually pick Light).
