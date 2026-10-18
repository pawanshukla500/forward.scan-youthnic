import {
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ClipboardList,
  Copy,
  Database,
  Download,
  ExternalLink,
  KeyRound,
  Pencil,
  Plus,
  QrCode,
  RefreshCw,
  Smartphone,
  Store,
  UserCheck,
  Users as UsersIcon,
  UserX,
  Warehouse,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, fmtDateTime, PASSWORD_RULE, passwordProblem, type Channel, type Role } from "../api";
import { useAuth } from "../App";
import { initials } from "../components/Shell";
import { Button, Card, ChannelDot, cx, Empty, Field, inputCls, Spinner, Toast, SkeletonRows } from "../components/ui";

type Tab = "overview" | "channels" | "warehouses" | "sync";

const TABS: [Tab, string][] = [
  ["overview", "Overview & users"],
  ["channels", "Sales channels"],
  ["warehouses", "Warehouses"],
  ["sync", "OMSGuru sync"],
];


interface OmsIncident {
  kind: "down" | "key_refused";
  start: number;
  end: number | null;
  error?: string;
}

/** OMSGuru connection as the sync sees it, with the history of outages / refused keys. */
function OmsConnectionCard({ o }: { o: { state?: string; since?: number; last_ok?: number; checked_at?: number; last_error?: string; incidents?: OmsIncident[] } }) {
  const t = (x?: number | null) =>
    x ? new Date(x * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "-";
  const mins = (a: number, b: number | null) => {
    const m = Math.round(((b ?? Date.now() / 1000) - a) / 60);
    return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
  };
  const meta =
    o.state === "down"
      ? { label: "OMSGuru is down (their outage)", cls: "bg-crit-wash text-crit-ink" }
      : o.state === "key_refused"
        ? { label: "Our API key is refused - update the OMSGuru API token", cls: "bg-crit-wash text-crit-ink" }
        : { label: "Working", cls: "bg-good-wash text-good-ink" };
  const kindLabel = (k: string) => (k === "key_refused" ? "API key refused" : "OMSGuru down");
  return (
    <Card className="mt-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">OMSGuru connection</h2>
        <span className={cx("rounded-md px-2 py-0.5 text-xs font-semibold", meta.cls)}>{meta.label}</span>
      </div>
      <div className="mt-1 text-xs text-muted">
        {o.state && o.state !== "ok" ? `Since ${t(o.since)} (${mins(o.since ?? 0, null)})` : `Last successful call ${t(o.last_ok)}`} · checked {t(o.checked_at)}
      </div>
      {o.last_error && <div className="mt-1 text-xs text-crit-ink">{o.last_error}</div>}
      <div className="mt-3 text-xs font-medium text-ink-2">Recent outages</div>
      {o.incidents && o.incidents.length > 0 ? (
        <ul className="mt-1 space-y-1 text-xs">
          {o.incidents.map((x, i) => (
            <li key={i} className="flex flex-wrap gap-x-2">
              <b className={x.end ? "text-ink-2" : "text-crit-ink"}>{kindLabel(x.kind)}</b>
              <span className="tnum">
                {t(x.start)} - {x.end ? t(x.end) : "now"} ({mins(x.start, x.end)})
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="mt-1 text-xs text-muted">None recorded.</div>
      )}
      <div className="mt-2 text-xs text-muted">
        While OMSGuru is unavailable, scans use the stored orders and the sync checks every 30 s. When it works again the app continues from
        where it stopped and re-checks the last 7 days, so nothing is missed.
      </div>
    </Card>
  );
}

export default function Admin() {
  const [tab, setTab] = useState<Tab>("overview");
  const [toast, setToast] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);
  const notify = useCallback((kind: "ok" | "err", msg: string) => setToast({ kind, msg }), []);
  return (
    <div className="space-y-5">
      <div className="flex gap-1 overflow-x-auto overflow-y-hidden border-b border-line" role="tablist" aria-label="Admin sections">
        {TABS.map(([k, l]) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={cx(
              "-mb-px min-h-11 cursor-pointer whitespace-nowrap border-b-2 px-4 text-sm font-semibold",
              tab === k ? "border-accent text-accent-ink" : "border-transparent text-muted hover:text-ink",
            )}
          >
            {l}
          </button>
        ))}
      </div>
      {tab === "overview" && <Overview notify={notify} go={setTab} />}
      {tab === "channels" && <Channels notify={notify} />}
      {tab === "warehouses" && <Warehouses notify={notify} />}
      {tab === "sync" && <Sync notify={notify} />}
      {toast && (
        <Toast kind={toast.kind} onClose={() => setToast(null)}>
          {toast.msg}
        </Toast>
      )}
    </div>
  );
}

type Notify = (kind: "ok" | "err", msg: string) => void;

/* ---- overview: system health from the real sync engine ------------------------------------- */

function ago(sec: number | null | undefined): string {
  if (!sec) return "never";
  const s = Math.max(0, Date.now() / 1000 - sec);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

function uptime(sec: number | null): string {
  if (!sec) return "";
  const s = Date.now() / 1000 - sec;
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

function SystemCard({ icon, title, state, tone, meta, children }: { icon: ReactNode; title: string; state: string; tone: "good" | "warn" | "crit" | "muted"; meta: string; children?: ReactNode }) {
  return (
    <div className="card system-card">
      <div className="system-icon" aria-hidden>
        {icon}
      </div>
      <span className={cx("status", tone === "crit" ? "error" : tone === "warn" ? "warn" : "success")}>
        <i />
        {state}
      </span>
      <h3>{title}</h3>
      <p>{meta}</p>
      {children ?? (
        <div className="mini-line" aria-hidden>
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
          <i />
        </div>
      )}
    </div>
  );
}

function Overview({ notify, go }: { notify: Notify; go: (t: Tab) => void }) {
  const [s, setS] = useState<SyncStatus | null>(null);
  const [bk, setBk] = useState<BackupStatus | null>(null);
  const [health, setHealth] = useState<{ mode: string; started_at: number; ws_clients: number } | null>(null);
  const [checked, setChecked] = useState<number | null>(null);
  const [counts, setCounts] = useState<{ channels: number; scanning: number; warehouses: number; syncing: number } | null>(null);
  const [countFrom, setCountFrom] = useState<string | null | undefined>(undefined);
  const usersRef = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    Promise.all([
      api<SyncStatus>("/api/admin/sync").then(setS),
      api<{ mode: string; started_at: number; ws_clients: number }>("/api/health").then(setHealth),
      api<BackupStatus>("/api/admin/backups").then(setBk).catch(() => setBk(null)),
    ])
      .then(() => setChecked(Date.now()))
      .catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [load]);
  useEffect(() => {
    Promise.all([
      api<{ channels: Channel[] }>("/api/admin/channels"),
      api<{ warehouses: { sync_enabled: boolean }[] }>("/api/admin/warehouses"),
      api<{ date: string | null }>("/api/admin/tracking-start").then((r) => setCountFrom(r.date)),
    ])
      .then(([c, w]) =>
        setCounts({
          channels: c.channels.length,
          scanning: c.channels.filter((x) => x.scan_enabled).length,
          warehouses: w.warehouses.length,
          syncing: w.warehouses.filter((x) => x.sync_enabled).length,
        }),
      )
      .catch(() => {});
  }, []);

  const inv = s?.jobs?.find((j) => j.name === "invoices");
  const failed = s?.jobs?.filter((j) => j.last_ok === false) ?? [];
  const lim = s?.limiter;
  const starving = !!lim && (lim.waiting_for_credit || lim.remaining_estimated < 2);
  const stale = !!inv && (!inv.last_finished || Date.now() / 1000 - inv.last_finished > 10 * 60);
  const problems = !s ? [] : !s.enabled ? ["OMSGuru sync is turned off"] : [
    ...failed.map((j) => `${JOB_LABEL[j.name] ?? j.name} failed`),
    ...(starving ? ["OMSGuru API credits are used up - scans fall back to the local copy"] : []),
    ...(stale ? ["No new AWBs synced in the last 10 minutes"] : []),
    ...(s.crosscheck && !s.crosscheck.ok && !s.crosscheck.retry ? ["Pending orders differ from OMSGuru - see OMSGuru sync"] : []),
    ...(bk?.problems ?? []),
  ];
  const bkTone = backupTone(bk);
  const live = s?.live_lookup;
  const liveTotal = live ? Object.values(live.outcomes).reduce((a, b) => a + b, 0) : 0;
  const livePct = live && liveTotal ? Math.round((100 * (live.outcomes.fresh ?? 0)) / liveTotal) : null;
  const credPct = lim ? Math.round((100 * lim.remaining_estimated) / lim.limit) : 0;

  const settings: { name: string; desc: string; meta: string; icon: ReactNode; onClick: () => void }[] = [
    {
      name: "Sales channels",
      desc: "Map OMSGuru channel labels, scan on/off, colours",
      meta: counts ? `${counts.scanning} scanning of ${counts.channels}` : "...",
      icon: <Store className="size-5" />,
      onClick: () => go("channels"),
    },
    {
      name: "Warehouses",
      desc: "Which dispatch warehouses are synced from OMSGuru",
      meta: counts ? `${counts.syncing} synced of ${counts.warehouses}` : "...",
      icon: <Warehouse className="size-5" />,
      onClick: () => go("warehouses"),
    },
    {
      name: "Count orders from",
      desc: "Pending and reconciliation count AWBs generated from this day",
      meta: countFrom === undefined ? "..." : countFrom ? fmtDay(countFrom) : "Not set",
      icon: <CalendarDays className="size-5" />,
      onClick: () => go("sync"),
    },
    {
      name: "OMSGuru sync",
      desc: "Jobs, API credits, live lookups, run a sync now",
      meta: s ? `every ${s.interval_seconds}s` : "...",
      icon: <RefreshCw className="size-5" />,
      onClick: () => go("sync"),
    },
    {
      name: "Users & roles",
      desc: "Scanner, supervisor, manager and admin access",
      meta: "Team members",
      icon: <UsersIcon className="size-5" />,
      onClick: () => usersRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
    },
    {
      name: "Data retention",
      desc: `Unscanned orders kept ${s?.retain_orders_days ?? 7} days; scans and scanned orders ${retentionText(s)}`,
      meta: s ? `${(s.cached_open_orders + (s.cached_left_orders ?? 0)).toLocaleString("en-IN")} orders stored` : "...",
      icon: <Database className="size-5" />,
      onClick: () => go("sync"),
    },
  ];

  return (
    <div className="admin-page">
      <div className="system-banner" role="status">
        <div>
          <span className="pulse">
            <i />
          </span>
          <div>
            <b>{!s ? "Checking systems..." : problems.length ? "Attention needed" : "All systems operational"}</b>
            <span>
              {problems.length ? problems.join(" · ") : `Last checked ${checked ? new Date(checked).toLocaleTimeString("en-IN") : "-"} · Auto-refresh every 15 seconds`}
            </span>
          </div>
        </div>
        <span>{health?.mode === "mock" ? "Demo data" : health ? `Up ${uptime(health.started_at)}` : "Live OMSGuru"}</span>
      </div>

      <div className="system-grid">
        {!s || !s.enabled ? (
          [0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-[170px] rounded-[14px]" />)
        ) : (
          <>
            <SystemCard
              icon={<Zap className="size-5" />}
              title="OMSGuru API"
              state={
                s.omsguru?.state === "down"
                  ? "OMSGuru down"
                  : s.omsguru?.state === "key_refused"
                    ? "Key refused"
                    : starving
                      ? "Throttled"
                      : lim?.last_error
                        ? "Errors"
                        : "Operational"
              }
              tone={s.omsguru?.state === "down" || s.omsguru?.state === "key_refused" || lim?.last_error ? "crit" : starving ? "warn" : "good"}
              meta={`${Math.floor(lim!.remaining_estimated)} of ${lim!.limit} credits free · ${lim!.calls_total} calls`}
            >
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-line" role="meter" aria-valuenow={credPct} aria-valuemin={0} aria-valuemax={100} aria-label="API credits free">
                <div className={cx("h-full rounded-full", credPct < 15 ? "bg-crit" : credPct < 40 ? "bg-warn" : "bg-accent")} style={{ width: `${credPct}%` }} />
              </div>
            </SystemCard>
            <SystemCard
              icon={<RefreshCw className="size-5" />}
              title="Order sync"
              state={inv?.running ? "Running" : inv?.last_ok === false ? "Failed" : stale ? "Delayed" : "Live"}
              tone={inv?.last_ok === false ? "crit" : stale ? "warn" : "good"}
              meta={`Last run ${ago(inv?.last_finished)} · ${s.cached_open_orders.toLocaleString("en-IN")} ready to ship`}
            />
            <SystemCard
              icon={<CheckCircle2 className="size-5" />}
              title="Live lookup per scan"
              state={live?.enabled ? "On" : "Off"}
              tone={live?.enabled ? "good" : "muted"}
              meta={livePct === null ? "No scans since start" : `${livePct}% of ${liveTotal.toLocaleString("en-IN")} scans fetched live`}
            />
            <SystemCard
              icon={<Database className="size-5" />}
              title="Database & backups"
              state={
                bkTone === "good" ? "Protected" : bkTone === "warn" ? (bk?.backend === "postgresql" ? "Only on this server" : "Only on this PC") : bkTone === "muted" ? "No backups" : "At risk"
              }
              tone={bkTone}
              meta={backupMeta(bk)}
            />
          </>
        )}
      </div>

      <MobileAppCard notify={notify} />

      <div ref={usersRef} className="scroll-mt-24">
        <Users notify={notify} />
      </div>

      <section className="admin-settings">
        <div className="settings-heading">
          <span className="eyebrow">Workspace configuration</span>
          <h2>Operations setup</h2>
          <p>Manage infrastructure, rules and integrations.</p>
        </div>
        <div className="settings-grid">
          {settings.map((x) => (
            <button key={x.name} type="button" onClick={x.onClick} className="setting-card card">
              <span className="system-icon" aria-hidden>
                {x.icon}
              </span>
              <div>
                <b>{x.name}</b>
                <span>{x.desc}</span>
              </div>
              <strong>{x.meta}</strong>
              <ChevronRight className="size-4" aria-hidden />
            </button>
          ))}
          <Link to="/scans" className="setting-card card">
            <span className="system-icon" aria-hidden>
              <ClipboardList className="size-4" />
            </span>
            <div>
              <b>Reports & audit log</b>
              <span>Every scan, rejected attempt and flag</span>
            </div>
            <strong>Excel / CSV</strong>
            <ChevronRight className="size-4" aria-hidden />
          </Link>
        </div>
      </section>
    </div>
  );
}

/* ---- Android app: public download link + QR (served by routers/mobile_app.py) ------------------ */

interface AppRelease {
  available: boolean;
  version_code?: number;
  version_name?: string;
  notes?: string;
  size?: number;
  published_at?: string;
  min_version_code?: number;
  download_url?: string;
  page_url: string;
  qr_url: string;
}

function MobileAppCard({ notify }: { notify: Notify }) {
  const [r, setR] = useState<AppRelease | null>(null);
  useEffect(() => {
    api<AppRelease>("/api/app/latest").then(setR).catch(() => setR(null));
  }, []);
  if (!r) return null;

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(r!.page_url);
      notify("ok", "Download link copied - paste it in WhatsApp or anywhere");
    } catch {
      notify("err", `Copy failed - the link is ${r!.page_url}`);
    }
  }

  const published = r.published_at ? new Date(r.published_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : null;
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <Smartphone className="size-5 text-accent-ink" aria-hidden />
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">Android app</h2>
          <p className="text-xs text-muted">Anyone can scan the QR or open the link to install the scanner app - no sign-in needed to download.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => void copyLink()}>
            <Copy className="size-4" aria-hidden /> Copy link
          </Button>
          <a href={r.page_url} target="_blank" rel="noreferrer" className="ease-ui inline-flex h-9 items-center gap-2 rounded-lg border border-line-strong bg-surface px-3 text-sm font-medium text-ink hover:bg-surface-2">
            <ExternalLink className="size-4" aria-hidden /> Open page
          </a>
        </div>
      </div>
      <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-start">
        <a href={r.qr_url} target="_blank" rel="noreferrer" title="Open the QR code on its own (to print it)" className="shrink-0 self-center sm:self-start">
          <img src={r.qr_url} alt={`QR code for ${r.page_url}`} width={168} height={168} className="size-[168px] rounded-lg border border-line bg-white" />
        </a>
        <div className="min-w-0 flex-1 space-y-3 text-sm">
          <div>
            <div className="text-xs text-muted">Public download link</div>
            <div className="break-all font-mono text-sm font-semibold">{r.page_url}</div>
          </div>
          {r.available ? (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                <div>
                  <div className="text-xs text-muted">Latest version</div>
                  <div className="font-semibold">
                    {r.version_name} <span className="text-xs font-normal text-muted">({r.version_code})</span>
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted">Published</div>
                  <div className="font-semibold">{published ?? "-"}</div>
                </div>
                <div>
                  <div className="text-xs text-muted">Size</div>
                  <div className="font-semibold">{r.size ? `${(r.size / 1048576).toFixed(1)} MB` : "-"}</div>
                </div>
              </div>
              {r.notes && <p className="whitespace-pre-line text-xs text-muted">What is new: {r.notes}</p>}
              <div className="flex flex-wrap items-center gap-2">
                <a href={r.download_url} className="ease-ui inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-3 text-sm font-semibold text-on-accent hover:bg-accent-hover">
                  <Download className="size-4" aria-hidden /> Download APK
                </a>
                <span className="text-xs text-muted">
                  <QrCode className="mr-1 inline size-3.5 align-[-2px]" aria-hidden />
                  Installed phones are told about new versions automatically and update from inside the app.
                </span>
              </div>
            </>
          ) : (
            <p className="text-sm text-warn-ink">
              No app has been published to this server yet. On GitHub run Actions &rarr; <b>Build Android APK</b> (it also runs by itself when the app
              code changes) - it uploads the APK here and this card shows the version.
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}

/* ---- users ---------------------------------------------------------------------------------- */

interface UserRow {
  id: number;
  username: string;
  full_name: string;
  email: string;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  password_changed_at: string | null;
}

const ROLE_LABEL: Record<Role, string> = { scanner: "Scan operator", supervisor: "Supervisor", manager: "Manager", admin: "Admin" };

/** 12 characters without look-alikes (0/O, 1/l/I), always with letters and digits - easy to read out to someone. */
function generatePassword(): string {
  const letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ";
  const digits = "23456789";
  const all = letters + digits;
  const pick = (set: string) => set[crypto.getRandomValues(new Uint32Array(1))[0] % set.length];
  const chars = [pick(letters), pick(digits), ...Array.from({ length: 10 }, () => pick(all))];
  for (let i = chars.length - 1; i > 0; i--) {
    const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

function Dialog({ title, sub, onClose, children }: { title: string; sub?: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-[rgba(4,12,8,0.58)] p-4 backdrop-blur-sm" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()} className="card flash-in max-h-[92vh] w-full max-w-md space-y-4 overflow-y-auto p-6" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div className="flex items-start justify-between gap-3">
          <div>
            <span className="eyebrow">Access management</span>
            <h2 id="dlg-title" className="mt-1 text-xl font-bold">
              {title}
            </h2>
            {sub && <p className="text-sm text-muted">{sub}</p>}
          </div>
          <button type="button" onClick={onClose} className="grid size-10 cursor-pointer place-items-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close">
            <X className="size-5" aria-hidden />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Password input with Show and Generate; the admin shares the result with the person directly. */
function PasswordInput({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const [show, setShow] = useState(false);
  return (
    <div className="flex gap-2">
      <input
        id={id}
        className={cx(inputCls, "min-w-0 flex-1 font-mono")}
        type={show ? "text" : "password"}
        autoComplete="new-password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <Button type="button" size="sm" onClick={() => setShow((v) => !v)} aria-pressed={show}>
        {show ? "Hide" : "Show"}
      </Button>
      <Button
        type="button"
        size="sm"
        onClick={() => {
          onChange(generatePassword());
          setShow(true);
        }}
      >
        Generate
      </Button>
    </div>
  );
}

/** [scannerOnly]: a manager or supervisor - the new account is always a scan operator. */
function CreateUser({ onClose, onCreated, notify, scannerOnly }: { onClose: () => void; onCreated: () => void; notify: Notify; scannerOnly: boolean }) {
  const [form, setForm] = useState({ full_name: "", email: "", username: "", password: "", role: "scanner" as Role, must_change_password: true });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const suggested = (form.email.split("@")[0] || form.full_name).trim().toLowerCase().replace(/[^a-z0-9._-]+/g, ".").replace(/^\.+|\.+$/g, "");
  const username = form.username || suggested;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = !username ? "Enter a username" : passwordProblem(form.password);
    if (problem) {
      setErr(problem);
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await api("/api/admin/users", { method: "POST", json: { ...form, username } });
      notify("ok", `${form.full_name || username} can now sign in with ${form.email || username} and the password you set`);
      onCreated();
      onClose();
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={scannerOnly ? "Add scanner ID" : "Create team member"}
      sub={scannerOnly ? "A login for a packer: scan only. Ask an admin for supervisor or manager access." : "Add a person and choose what they can do in ForwardScan."}
      onClose={onClose}
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <Field label="Full name">
          <input id="nu-name" autoFocus className={inputCls} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} placeholder="e.g. Ravi Kumar" />
        </Field>
        <Field label="Email (optional)" hint="They can sign in with it; packers without email use the username">
          <input id="nu-email" className={inputCls} type="email" autoComplete="off" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="name@vbexports.co.in" />
        </Field>
        <Field label="Username (login id)" hint="Letters, numbers, dot, dash or underscore">
          <input
            id="nu-username"
            className={inputCls}
            autoComplete="off"
            value={form.username}
            placeholder={suggested || "e.g. ravi.k"}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
          />
        </Field>
        <Field label="Password" hint={PASSWORD_RULE}>
          <PasswordInput id="nu-password" value={form.password} onChange={(password) => setForm({ ...form, password })} />
        </Field>
        <Field label="Role">
          {scannerOnly ? (
            <p className={cx(inputCls, "flex items-center text-ink-2")}>Scan operator - scan only</p>
          ) : (
            <select id="nu-role" className={inputCls} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
              <option value="scanner">Scan operator - scan only</option>
              <option value="supervisor">Supervisor - reports, exports, remove scans, scanner IDs</option>
              <option value="manager">Manager - same as supervisor (warehouse manager)</option>
              <option value="admin">Admin - everything incl. users and settings</option>
            </select>
          )}
        </Field>
        <label className="flex cursor-pointer items-start gap-2 text-sm text-ink-2">
          <input id="nu-must" type="checkbox" className="mt-0.5 size-4" checked={form.must_change_password} onChange={(e) => setForm({ ...form, must_change_password: e.target.checked })} />
          Ask them to choose their own password the first time they sign in
        </label>
        {err && (
          <p className="rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink" role="alert">
            {err}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            {scannerOnly ? "Add scanner" : "Create user"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function EditUser({ u, onClose, onSaved, notify }: { u: UserRow; onClose: () => void; onSaved: () => void; notify: Notify }) {
  const [form, setForm] = useState({ full_name: u.full_name, email: u.email });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: form });
      notify("ok", `${form.full_name || u.username} updated`);
      onSaved();
      onClose();
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={`Edit ${u.full_name || u.username}`} sub={`Username ${u.username} stays the same.`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <Field label="Full name">
          <input id="eu-name" autoFocus className={inputCls} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
        </Field>
        <Field label="Email (optional)">
          <input id="eu-email" className={inputCls} type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        {err && (
          <p className="rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink" role="alert">
            {err}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            Save
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ResetPassword({ u, onClose, onSaved, notify }: { u: UserRow; onClose: () => void; onSaved: () => void; notify: Notify }) {
  const [password, setPassword] = useState("");
  const [must, setMust] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = passwordProblem(password);
    if (problem) {
      setErr(problem);
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: { password, must_change_password: must } });
      notify("ok", `Password reset for ${u.full_name || u.username} - their other sessions are signed out`);
      onSaved();
      onClose();
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={`Reset password`} sub={`For ${u.full_name || u.username} (${u.email || u.username}). Share the new password with them directly.`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
        <Field label="New password" hint={PASSWORD_RULE}>
          <PasswordInput id="rp-password" value={password} onChange={setPassword} />
        </Field>
        <label className="flex cursor-pointer items-start gap-2 text-sm text-ink-2">
          <input id="rp-must" type="checkbox" className="mt-0.5 size-4" checked={must} onChange={(e) => setMust(e.target.checked)} />
          Ask them to choose their own password when they next sign in
        </label>
        {err && (
          <p className="rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink" role="alert">
            {err}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            Reset password
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Users({ notify }: { notify: Notify }) {
  const { user: me } = useAuth();
  const isAdmin = me?.role === "admin";
  // managers and supervisors: add scanner IDs, reset a scanner's password, disable / enable a scanner
  const isStaff = isAdmin || me?.role === "manager" || me?.role === "supervisor";
  const canManage = (u: UserRow) => isAdmin || (isStaff && u.role === "scanner");
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const load = useCallback(() => api<{ users: UserRow[] }>("/api/admin/users").then((r) => setUsers(r.users)), []);
  useEffect(() => void load(), [load]);

  async function patch(u: UserRow, body: Record<string, unknown>, ok: string) {
    try {
      await api(`/api/admin/users/${u.id}`, { method: "PATCH", json: body });
      notify("ok", ok);
      void load();
    } catch (err) {
      notify("err", (err as Error).message);
    }
  }

  return (
    <section className="card users-card" aria-labelledby="team-title">
      <div className="card-head">
        <div>
          <span className="eyebrow">Access management</span>
          <h2 id="team-title" className="mt-1 text-[17px] font-bold">
            Team members
          </h2>
          <p className="text-sm text-muted">
            {isAdmin ? "Who can sign in, what they can do, and their passwords." : "Add scanner IDs, reset a scanner's password or disable a scanner who left."}
          </p>
        </div>
        {isStaff && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus className="size-4" aria-hidden /> {isAdmin ? "Create user" : "Add scanner"}
          </Button>
        )}
      </div>
      {!users ? (
        <SkeletonRows />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-y border-line bg-surface-2 text-left text-xs font-bold uppercase tracking-wide text-muted">
                <th scope="col" className="px-5 py-2.5 sm:px-6">User</th>
                <th scope="col" className="px-3 py-2.5">Role</th>
                <th scope="col" className="px-3 py-2.5">Last sign-in</th>
                <th scope="col" className="px-3 py-2.5">Password</th>
                <th scope="col" className="px-3 py-2.5">Status</th>
                {isStaff && <th scope="col" className="px-3 py-2.5"><span className="sr-only">Actions</span></th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {users.map((u) => (
                <tr key={u.id} className={cx(!u.is_active && "opacity-60")}>
                  <td className="px-5 py-3 sm:px-6">
                    <span className="flex items-center gap-3">
                      <span className="grid size-9 shrink-0 place-items-center rounded-full bg-accent-wash text-xs font-bold text-accent-ink" aria-hidden>
                        {initials(u.full_name || u.username)}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate font-semibold">
                          {u.full_name || u.username}
                          {u.id === me?.id && <span className="ml-1.5 text-xs font-normal text-muted">(you)</span>}
                        </span>
                        <span className="block truncate text-xs text-muted">
                          {u.username}
                          {u.email && ` · ${u.email}`}
                        </span>
                      </span>
                    </span>
                  </td>
                  <td className="px-3 py-3">
                    {isAdmin && u.id !== me?.id ? (
                      <select
                        className={`${inputCls} h-9 w-auto`}
                        value={u.role}
                        onChange={(e) => void patch(u, { role: e.target.value }, `${u.full_name || u.username} is now ${ROLE_LABEL[e.target.value as Role]}`)}
                        aria-label={`Role for ${u.username}`}
                      >
                        <option value="scanner">Scan operator</option>
                        <option value="supervisor">Supervisor</option>
                        <option value="manager">Manager</option>
                        <option value="admin">Admin</option>
                      </select>
                    ) : (
                      <span>{ROLE_LABEL[u.role]}</span>
                    )}
                  </td>
                  <td className="px-3 py-3 text-ink-2">{u.last_login_at ? fmtDateTime(u.last_login_at) : "Never"}</td>
                  <td className="px-3 py-3">
                    {u.must_change_password ? (
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-warn-wash px-2.5 py-1 text-xs font-bold text-warn-ink">
                        <KeyRound className="size-3.5" aria-hidden /> Must choose a new one
                      </span>
                    ) : (
                      <span className="text-ink-2">{u.password_changed_at ? `Set ${fmtDateTime(u.password_changed_at)}` : "Set"}</span>
                    )}
                  </td>
                  <td className="px-3 py-3">
                    <span
                      className={cx(
                        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold",
                        u.is_active ? "bg-good-wash text-good-ink" : "bg-surface-2 text-ink-2",
                      )}
                    >
                      <span className={cx("size-1.5 rounded-full", u.is_active ? "bg-good" : "bg-muted")} aria-hidden />
                      {u.is_active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  {isStaff && (
                    <td className="px-3 py-3">
                      <div className="flex justify-end gap-1">
                        {isAdmin && (
                          <Button size="sm" variant="ghost" title="Edit name / email" aria-label={`Edit ${u.username}`} onClick={() => setEditing(u)}>
                            <Pencil className="size-4" aria-hidden />
                          </Button>
                        )}
                        {canManage(u) && (
                          <Button size="sm" variant="ghost" title="Reset password" aria-label={`Reset password for ${u.username}`} onClick={() => setResetting(u)}>
                            <KeyRound className="size-4" aria-hidden />
                          </Button>
                        )}
                        {canManage(u) && u.id !== me?.id && (
                          <Button
                            size="sm"
                            variant="ghost"
                            title={u.is_active ? "Disable" : "Enable"}
                            aria-label={`${u.is_active ? "Disable" : "Enable"} ${u.username}`}
                            onClick={() => void patch(u, { is_active: !u.is_active }, u.is_active ? `${u.full_name || u.username} can no longer sign in` : `${u.full_name || u.username} can sign in again`)}
                          >
                            {u.is_active ? <UserX className="size-4" aria-hidden /> : <UserCheck className="size-4" aria-hidden />}
                          </Button>
                        )}
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating && <CreateUser onClose={() => setCreating(false)} onCreated={() => void load()} notify={notify} scannerOnly={!isAdmin} />}
      {editing && <EditUser u={editing} onClose={() => setEditing(null)} onSaved={() => void load()} notify={notify} />}
      {resetting && <ResetPassword u={resetting} onClose={() => setResetting(null)} onSaved={() => void load()} notify={notify} />}
    </section>
  );
}

interface AdminChannel extends Channel {
  aliases: string;
  cached_orders: number;
}

function Channels({ notify }: { notify: Notify }) {
  const { user: me } = useAuth();
  const isAdmin = me?.role === "admin";
  const [rows, setRows] = useState<AdminChannel[] | null>(null);
  const [unmapped, setUnmapped] = useState<{ label: string; company: string; orders: number }[]>([]);
  const load = useCallback(() => {
    api<{ channels: AdminChannel[] }>("/api/admin/channels").then((r) => setRows(r.channels));
    api<{ labels: { label: string; company: string; orders: number }[] }>("/api/admin/unmapped-labels").then((r) => setUnmapped(r.labels));
  }, []);
  useEffect(load, [load]);

  async function patch(c: AdminChannel, body: Record<string, unknown>) {
    try {
      const r = await api<{ remapped_orders: number }>(`/api/admin/channels/${c.id}`, { method: "PATCH", json: body });
      notify("ok", r.remapped_orders ? `Saved - ${r.remapped_orders} orders re-mapped` : "Saved");
      load();
    } catch (e) {
      notify("err", (e as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      {unmapped.length > 0 && (
        <Card className="border-warn p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-warn-ink">
            <CircleAlert className="size-4" aria-hidden /> Orders whose OMS channel name is not mapped
          </div>
          <p className="mt-1 text-sm text-ink-2">Scans of these orders can't be checked for the right marketplace. Copy the label into the matching channel's "Also known as" box.</p>
          <ul className="mt-2 space-y-1 text-sm">
            {unmapped.map((u) => (
              <li key={u.label + u.company} className="font-mono">
                "{u.label}" <span className="text-muted">({u.company || "no company"}) · {u.orders} orders</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card>
        {!rows ? (
          <SkeletonRows />
        ) : rows.length === 0 ? (
          <Empty>No channels yet - they load from OMSGuru on the first sync (see OMSGuru sync tab).</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2 font-medium">Sales channel (OMSGuru)</th>
                  <th className="px-3 py-2 font-medium">OMS status</th>
                  <th className="px-3 py-2 font-medium">Scanning</th>
                  <th className="px-3 py-2 font-medium">Colour</th>
                  <th className="px-3 py-2 font-medium">Order</th>
                  <th className="px-3 py-2 text-right font-medium">Cached orders</th>
                  <th className="px-3 py-2 font-medium">Also known as (one per line)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((c) => (
                  <tr key={c.id} className="align-top">
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <ChannelDot color={c.color} />
                        <div>
                          <div className="font-medium">{c.name}</div>
                          <div className="text-xs text-muted">
                            {c.marketplace} · id {c.id}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 capitalize text-ink-2">{c.oms_status}</td>
                    <td className="px-3 py-2.5">
                      <label className="inline-flex items-center gap-2">
                        <input type="checkbox" className="size-4" disabled={!isAdmin} checked={c.scan_enabled} onChange={(e) => void patch(c, { scan_enabled: e.target.checked })} />
                        {c.scan_enabled ? "On" : "Off"}
                      </label>
                    </td>
                    <td className="px-3 py-2.5">
                      <input type="color" disabled={!isAdmin} value={c.color || "#898781"} onChange={(e) => void patch(c, { color: e.target.value })} className="h-8 w-10 cursor-pointer rounded border border-line bg-transparent" aria-label={`Colour for ${c.name}`} />
                    </td>
                    <td className="px-3 py-2.5">
                      <input type="number" disabled={!isAdmin} defaultValue={c.sort_order} onBlur={(e) => Number(e.target.value) !== c.sort_order && void patch(c, { sort_order: Number(e.target.value) })} className={`${inputCls} h-8 w-20`} aria-label={`Sort order for ${c.name}`} />
                    </td>
                    <td className="tnum px-3 py-2.5 text-right">{c.cached_orders.toLocaleString("en-IN")}</td>
                    <td className="px-3 py-2.5">
                      <textarea
                        disabled={!isAdmin}
                        defaultValue={c.aliases}
                        rows={1}
                        onBlur={(e) => e.target.value !== c.aliases && void patch(c, { aliases: e.target.value })}
                        className="min-h-8 w-full rounded-lg border border-line-strong bg-surface px-2 py-1 font-mono text-xs"
                        aria-label={`Alternate names for ${c.name}`}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function Warehouses({ notify }: { notify: Notify }) {
  const [rows, setRows] = useState<{ id: number; name: string; alias: string; sync_enabled: boolean }[] | null>(null);
  const [form, setForm] = useState({ id: "", name: "", alias: "" });
  const load = useCallback(() => api<{ warehouses: typeof rows }>("/api/admin/warehouses").then((r) => setRows(r.warehouses)), []);
  useEffect(() => void load(), [load]);

  async function save(body: { id: number; name: string; alias: string; sync_enabled: boolean }) {
    try {
      await api("/api/admin/warehouses", { method: "POST", json: body });
      notify("ok", "Warehouse saved");
      void load();
    } catch (e) {
      notify("err", (e as Error).message);
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card>
        <p className="border-b border-line px-4 py-3 text-sm text-ink-2">
          Open (Packed / Ready-to-ship) and cancelled orders are pulled per warehouse. They load from OMSGuru automatically; add one by id if it's missing.
        </p>
        {!rows ? (
          <SkeletonRows />
        ) : rows.length === 0 ? (
          <Empty>No warehouses yet.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((w) => (
              <li key={w.id} className="flex items-center justify-between gap-3 px-4 py-3 text-sm">
                <div>
                  <div className="font-medium">{w.name || `Warehouse ${w.id}`}</div>
                  <div className="text-xs text-muted">
                    id {w.id} {w.alias && `· ${w.alias}`}
                  </div>
                </div>
                <label className="inline-flex items-center gap-2">
                  <input type="checkbox" className="size-4" checked={w.sync_enabled} onChange={(e) => void save({ ...w, sync_enabled: e.target.checked })} />
                  Sync
                </label>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card className="p-4">
        <h2 className="mb-3 text-sm font-semibold">Add warehouse</h2>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void save({ id: Number(form.id), name: form.name, alias: form.alias, sync_enabled: true });
            setForm({ id: "", name: "", alias: "" });
          }}
        >
          <Field label="OMSGuru warehouse id">
            <input className={inputCls} required type="number" min={1} value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
          </Field>
          <Field label="Name">
            <input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Alias / code">
            <input className={inputCls} value={form.alias} onChange={(e) => setForm({ ...form, alias: e.target.value })} />
          </Field>
          <Button type="submit" variant="primary" className="w-full">
            <Plus className="size-4" /> Save
          </Button>
        </form>
      </Card>
    </div>
  );
}

interface SyncStatus {
  mode: string;
  enabled: boolean;
  interval_seconds: number;
  lookback_days: number;
  cached_orders: number;
  cached_open_orders: number;
  cached_left_orders?: number;
  retain_orders_days?: number;
  scan_retention_years?: number;
  retention_day?: number;
  retention_next?: string | null;
  retention_last?: { at: string; kept_from: string; scans: number; orders: number; file: string | null; cloud: boolean | null } | null;
  history?: { done?: boolean; windows?: unknown[]; rows?: number };
  jobs: { name: string; last_started: number | null; last_finished: number | null; last_ok: boolean | null; last_message: string; running: boolean }[];
  limiter: {
    limit: number;
    remaining_reported: number | null;
    remaining_estimated: number;
    reserve: number;
    calls_total: number;
    throttled_total: number;
    errors_total: number;
    last_error: string;
    resolved_error?: string;
    last_error_at?: number | null;
    last_ok_at?: number | null;
    waiting_for_credit: boolean;
  };
  logs: { job: string; started_at: string; ok: boolean; calls: number; records: number; message: string }[];
  live_lookup?: { enabled: boolean; timeout_seconds: number; outcomes: Record<string, number>; methods?: LookupMethod[] };
  omsguru?: { state?: string; since?: number; last_ok?: number; checked_at?: number; last_error?: string; incidents?: OmsIncident[] };
  crosscheck?: CrossCheck | null;
  exit_check_pending?: number;
}

interface LookupMethod {
  channel_id: number | null;
  name: string;
  best: "sub" | "order" | null;
  sub: [number, number];
  order: [number, number];
}

interface CrossCheck {
  ok: boolean;
  retry: boolean;
  checked_at: number;
  refreshed_at: number | null;
  lookback_days: number;
  oms: number;
  local: number;
  unmapped_local: number;
  older_total: number;
  channels: { channel_id: number; name: string; color: string; oms: number; local: number; diff: number; older: number; ok: boolean }[];
}

const JOB_LABEL: Record<string, string> = {
  open_orders: "Packed + Ready-to-ship refresh",
  crosscheck: "Pending check against OMSGuru (after every refresh)",
  invoices: "New AWBs / invoices (every minute)",
  cancel_sweep: "Cancellation check (stored orders only)",
  audit: "Order-trail check: every AWB of today + yesterday vs OMSGuru both ways, adds missed ones (hourly; last 7 days once a night)",
  exit_check: "Status of orders that left RTS unscanned (spare credits only)",
  catch_up: "Catch-up after an OMSGuru outage: resumes from where it stopped, re-checks 7 days, refreshes the open list",
  history: "History backfill of earlier days (spare credits only)",
  cleanup: "Clean-up (unscanned orders older than the working set, every 15 min)",
  retention: "Monthly clean-up (the 10th): scanned data older than the retention, copy kept in backups",
  channels: "Sales channels",
};

/** "kept 2 years - older removed on the 10th of every month (next 10 Nov 2026)" */
function retentionText(s?: { scan_retention_years?: number; retention_day?: number; retention_next?: string | null } | null): string {
  const y = s?.scan_retention_years ?? 2;
  if (!y) return "kept forever";
  const d = s?.retention_day ?? 10;
  const th = d % 10 === 1 && d !== 11 ? "st" : d % 10 === 2 && d !== 12 ? "nd" : d % 10 === 3 && d !== 13 ? "rd" : "th";
  return `kept ${y} year${y > 1 ? "s" : ""} - older removed on the ${d}${th} of every month${s?.retention_next ? ` (next ${fmtDay(s.retention_next)})` : ""}`;
}

/* ---- the date orders are counted from (admin) ---------------------------------------------- */

function TrackingStartCard({ notify }: { notify: Notify }) {
  const { user } = useAuth();
  const [cur, setCur] = useState<{ date: string | null; today: string } | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ date: string | null; today: string }>("/api/admin/tracking-start").then((r) => {
      setCur(r);
      setValue(r.date ?? "");
    });
  }, []);

  async function save(date: string | null) {
    setBusy(true);
    try {
      const r = await api<{ date: string | null; today: string }>("/api/admin/tracking-start", { method: "PUT", json: { date } });
      setCur(r);
      setValue(r.date ?? "");
      notify("ok", r.date ? `Orders now count from ${fmtDay(r.date)}` : "Orders count from the whole kept window again");
    } catch (e) {
      notify("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!cur) return null;
  const isAdmin = user?.role === "admin";
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0 max-w-xl">
          <h2 className="text-sm font-semibold">Count orders from</h2>
          <p className="mt-1 text-xs text-muted">
            Only orders packed / made ready to ship (AWB generated) on or after this day count as pending, overdue or reconciled. Older orders still
            open in OMSGuru can be scanned, but are not counted.
          </p>
          <p className="mt-2 text-sm font-semibold">{cur.date ? `Counting from ${fmtDay(cur.date)}` : "Not set - every AWB of the kept days counts"}</p>
        </div>
        {isAdmin && (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void save(value || null);
            }}
          >
            <label className="text-xs font-medium text-ink-2" htmlFor="tracking-start">
              Start date
              <input
                id="tracking-start"
                type="date"
                className={cx(inputCls, "mt-1 block")}
                value={value}
                max={cur.today}
                onChange={(e) => setValue(e.target.value)}
              />
            </label>
            <Button type="submit" variant="primary" loading={busy} disabled={!value || value === cur.date}>
              Save
            </Button>
            {cur.date && (
              <Button type="button" variant="ghost" onClick={() => void save(null)} disabled={busy}>
                Clear
              </Button>
            )}
          </form>
        )}
      </div>
    </Card>
  );
}

function fmtDay(iso: string) {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

interface OffsiteCopy {
  remote: string;
  ok: boolean;
  at: string;
  file?: string;
  checked?: string;
  error?: string;
}

interface BackupStatus {
  enabled: boolean;
  reason?: string;
  backend?: "sqlite" | "postgresql";
  dir?: string;
  mirror_dir?: string | null;
  offsite?: { remote: string | null; full: OffsiteCopy | null; recent: OffsiteCopy | null; keep_days: number };
  full?: { started: string; ok: boolean; bytes: number; file: string; total_s: number; mirror?: { dir: string; ok: boolean } | null } | null;
  full_age_hours?: number | null;
  recent?: { started: string; ok: boolean; bytes: number; file: string; seconds: number } | null;
  recent_age_hours?: number | null;
  recent_minutes?: number;
  db_bytes?: number | null;
  wal_bytes?: number;
  disk_free_gb?: number | null;
  running?: string[];
  problems: string[];
}

// "Backups are only on this PC's / server's disk" (no second copy yet) is amber; every other backup problem is red.
const ONLY_LOCAL = "only on this";

function backupTone(b: BackupStatus | null): "good" | "warn" | "crit" | "muted" {
  if (!b) return "muted";
  if (!b.enabled) return b.reason ? "muted" : "crit";
  if (b.problems.some((p) => !p.includes(ONLY_LOCAL))) return "crit";
  return b.problems.length ? "warn" : "good";
}

function hoursAgo(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return "never";
  const m = Math.round(hours * 60);
  return m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(hours)} h ago` : `${Math.round(hours / 24)} days ago`;
}

function backupMeta(b: BackupStatus | null): string {
  if (!b) return "...";
  if (!b.enabled) return b.reason ?? "Automatic backups are off";
  const off = b.offsite?.full?.ok ? ` · offsite ${hoursAgo(ageHours(b.offsite.full.at))}` : "";
  return `Full backup ${hoursAgo(b.full_age_hours)} · recent scans ${hoursAgo(b.recent_age_hours)}${off}`;
}

function ageHours(iso: string | undefined): number | null {
  return iso ? (Date.now() - new Date(iso).getTime()) / 3600000 : null;
}

/** "gdrive:Forward Scan Backups/pg-forward_scan/daily" -> "Google Drive · Forward Scan Backups/pg-forward_scan/daily" */
function remoteLabel(remote: string): string {
  const [name, ...rest] = remote.split(":");
  const path = rest.join(":");
  return name === "gdrive" ? `Google Drive · ${path}` : remote;
}

const mb = (n: number | null | undefined) => (n ? `${(n / 1e6).toFixed(n > 1e8 ? 0 : 1)} MB` : "-");

function BackupCard({ notify }: { notify: Notify }) {
  const { user: me } = useAuth();
  const [b, setB] = useState<BackupStatus | null>(null);
  const load = useCallback(() => api<BackupStatus>("/api/admin/backups").then(setB).catch(() => setB(null)), []);
  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(t);
  }, [load]);

  async function run(kind: "full" | "recent") {
    try {
      await api(`/api/admin/backups/${kind}`, { method: "POST" });
      notify("ok", kind === "full" ? "Full backup started - it shows here when done" : "Copy of recent scans started");
    } catch (e) {
      notify("err", (e as Error).message);
    }
  }

  if (!b) return null;
  const tone = backupTone(b);
  const busy = !!b.running?.length;
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <Database
          className={cx("size-5", tone === "good" ? "text-good-ink" : tone === "warn" ? "text-warn-ink" : tone === "crit" ? "text-crit-ink" : "text-muted")}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">Database backups</h2>
          <p className="text-xs text-muted">{backupMeta(b)}</p>
        </div>
        {b.enabled && me?.role === "admin" && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => void run("recent")} disabled={busy}>
              Copy recent scans now
            </Button>
            <Button size="sm" variant="primary" onClick={() => void run("full")} disabled={busy}>
              {busy ? "Backing up..." : "Back up now"}
            </Button>
          </div>
        )}
      </div>
      <div className="grid gap-3 px-4 py-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <div className="text-xs text-muted">Full backup (daily, checked)</div>
          <div className="font-semibold">
            {b.full ? new Date(b.full.started).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "None yet"}
          </div>
          <div className="text-xs text-muted">{b.full ? `${mb(b.full.bytes)} compressed · ${b.full.total_s}s` : ""}</div>
        </div>
        <div>
          <div className="text-xs text-muted">Recent scans copy (every {b.recent_minutes} min)</div>
          <div className="font-semibold">{b.recent ? hoursAgo(b.recent_age_hours) : "None yet"}</div>
          <div className="text-xs text-muted">{b.recent ? mb(b.recent.bytes) : ""}</div>
        </div>
        <div>
          <div className="text-xs text-muted">Database</div>
          <div className="font-semibold">{mb(b.db_bytes)}</div>
          <div className="text-xs text-muted">{b.disk_free_gb !== null && b.disk_free_gb !== undefined ? `${b.disk_free_gb} GB free on the disk` : ""}</div>
        </div>
        <div className="min-w-0">
          <div className="text-xs text-muted">Offsite copy (daily, checked)</div>
          {b.offsite?.remote ? (
            <>
              <div className={cx("font-semibold", b.offsite.full && !b.offsite.full.ok && "text-crit-ink")}>
                {!b.offsite.full ? "Waiting for the first upload" : b.offsite.full.ok ? `Uploaded ${hoursAgo(ageHours(b.offsite.full.at))}` : "Upload failed"}
              </div>
              <div className="truncate text-xs text-muted" title={b.offsite.full?.remote ?? b.offsite.remote}>
                {remoteLabel(b.offsite.full?.remote ?? b.offsite.remote)} · kept {b.offsite.keep_days} days
              </div>
            </>
          ) : b.mirror_dir ? (
            <>
              <div className="font-semibold">Second disk</div>
              <div className="truncate text-xs text-muted" title={b.mirror_dir}>
                {b.mirror_dir}
              </div>
            </>
          ) : (
            <>
              <div className="font-semibold text-warn-ink">Not set</div>
              <div className="truncate text-xs text-muted">README: Backups &amp; restore - Offsite copy</div>
            </>
          )}
        </div>
      </div>
      {b.problems.length > 0 && (
        <ul className="space-y-1 border-t border-line px-4 py-3 text-xs">
          {b.problems.map((p) => (
            <li key={p} className={p.includes(ONLY_LOCAL) ? "text-warn-ink" : "text-crit-ink"}>
              {p}
            </li>
          ))}
        </ul>
      )}
      {b.enabled && (
        <p className="border-t border-line px-4 py-3 text-xs text-muted">
          Saved in <span className="font-mono">{b.dir}</span>.{" "}
          {b.backend === "postgresql" ? (
            <>
              To restore: stop the app container and run <span className="font-mono">restore_backup.py --latest --yes</span> in a one-off container
              (README: Backups &amp; restore).
            </>
          ) : (
            <>
              To restore: close the server, then from the project folder run{" "}
              <span className="font-mono">.venv\Scripts\python backend\restore_backup.py --latest --yes</span> (README: Backups &amp; restore).
            </>
          )}
        </p>
      )}
    </Card>
  );
}

function CrossCheckCard({ c, exitPending, onRun }: { c: CrossCheck | null | undefined; exitPending: number; onRun: () => void }) {
  const time = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "-");
  const [Icon, tone, title] = !c
    ? [RefreshCw, "text-muted", "Not checked yet"]
    : c.ok
      ? [CheckCircle2, "text-good-ink", "Pending orders match OMSGuru"]
      : c.retry
        ? [RefreshCw, "text-warn-ink", "Differs from OMSGuru - refreshing again within 5 minutes"]
        : [CircleAlert, "text-crit-ink", "Differs from OMSGuru"];
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <Icon className={cx("size-5", tone)} aria-hidden />
        <div className="min-w-0 flex-1">
          <h2 className={cx("text-sm font-semibold", tone)}>{title}</h2>
          <p className="text-xs text-muted">
            {c ? `Checked ${time(c.checked_at)}, after the Packed + Ready-to-ship refresh of ${time(c.refreshed_at)}` : "Runs after every Packed + Ready-to-ship refresh"}
          </p>
        </div>
        <Button size="sm" onClick={onRun}>
          Check now
        </Button>
      </div>
      <p className="px-4 pt-3 text-xs text-muted">
        OMSGuru's own pending report (order aging) against the local copy: Packed + Ready-to-ship orders created in the last {c?.lookback_days ?? 15} days, per
        sales channel. A difference of a couple of orders is normal (orders move while the refresh runs); anything bigger triggers an early re-refresh.
      </p>
      {c && (
        <div className="overflow-x-auto px-4 pb-3">
          <table className="mt-2 w-full min-w-[520px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-2 pr-3 font-medium">Sales channel</th>
                <th className="px-3 py-2 text-right font-medium">OMSGuru</th>
                <th className="px-3 py-2 text-right font-medium">Here</th>
                <th className="px-3 py-2 text-right font-medium">Difference</th>
              </tr>
            </thead>
            <tbody className="tnum divide-y divide-line">
              {c.channels.map((r) => (
                <tr key={r.channel_id}>
                  <td className="py-2 pr-3">
                    <span className="inline-flex items-center gap-2">
                      <ChannelDot color={r.color} />
                      {r.name}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right">{r.oms.toLocaleString("en-IN")}</td>
                  <td className="px-3 py-2 text-right">{r.local.toLocaleString("en-IN")}</td>
                  <td className={cx("px-3 py-2 text-right font-medium", r.ok ? "text-good-ink" : "text-crit-ink")}>
                    {r.diff === 0 ? "Match" : `${r.diff > 0 ? "+" : ""}${r.diff}`}
                    {!r.ok && " - check"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {c.unmapped_local > 0 && (
              <li className="text-crit-ink">{c.unmapped_local} orders here belong to an OMSGuru channel label that is not mapped - map it under Sales channels.</li>
            )}
            {c.older_total > 0 && (
              <li>
                {c.older_total.toLocaleString("en-IN")} Packed / Ready-to-ship orders in OMSGuru are older than {c.lookback_days} days and are not tracked (usually stuck orders; raise ORDER_LOOKBACK_DAYS to include them).
              </li>
            )}
            {exitPending > 0 && <li>{exitPending.toLocaleString("en-IN")} orders that left Ready-to-ship without a scan are waiting for their OMSGuru status (looked up on spare API credits).</li>}
          </ul>
        </div>
      )}
    </Card>
  );
}

function Sync({ notify }: { notify: Notify }) {
  const [s, setS] = useState<SyncStatus | null>(null);
  const load = useCallback(() => api<SyncStatus>("/api/admin/sync").then(setS), []);
  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(t);
  }, [load]);

  async function trigger(job: string) {
    try {
      await api(`/api/admin/sync/${job}`, { method: "POST" });
      notify("ok", `${JOB_LABEL[job]} queued`);
    } catch (e) {
      notify("err", (e as Error).message);
    }
  }

  if (!s) return <Spinner />;
  if (!s.enabled) return <Card className="p-4 text-sm">Sync is disabled (SYNC_ENABLED=false in .env).</Card>;
  const pct = Math.round((s.limiter.remaining_estimated / s.limiter.limit) * 100);
  return (
    <div className="space-y-4">
      <TrackingStartCard notify={notify} />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card className="p-4">
          <div className="text-xs font-medium text-ink-2">Mode</div>
          <div className="mt-1 text-xl font-semibold capitalize">{s.mode}</div>
          <div className="text-xs text-muted">invoice sync every {s.interval_seconds}s · {s.lookback_days}-day window</div>
        </Card>
        <Card className="p-4">
          <div className="text-xs font-medium text-ink-2">Packed / Ready-to-ship (synced)</div>
          <div className="mt-1 text-xl font-semibold">{s.cached_open_orders.toLocaleString("en-IN")}</div>
          <div className="text-xs text-muted">
            + {(s.cached_left_orders ?? 0).toLocaleString("en-IN")} other AWBs (unscanned kept {s.retain_orders_days ?? 7}d · scanned orders kept with their scans)
          </div>
          <div className="mt-1 text-xs text-muted">
            Scans {retentionText(s)}
            {s.retention_last && ` · last clean-up ${new Date(s.retention_last.at).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}: ${s.retention_last.scans.toLocaleString("en-IN")} scans from before ${fmtDay(s.retention_last.kept_from)} removed`}
            {s.history && !s.history.done ? ` · history backfill: ${(s.history.windows?.length ?? 0)} days left` : ""}
          </div>
        </Card>
        <Card className="p-4 sm:col-span-2">
          <div className="flex items-center justify-between text-xs font-medium text-ink-2">
            <span>OMSGuru API credits (60 per 5 min, shared by every integration on this account)</span>
          </div>
          <div className="mt-2 flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-info-wash" role="meter" aria-valuenow={s.limiter.remaining_estimated} aria-valuemin={0} aria-valuemax={s.limiter.limit} aria-label="API credits remaining">
              <div className={cx("h-full rounded-full", pct < 15 ? "bg-crit" : pct < 40 ? "bg-warn" : "bg-accent")} style={{ width: `${pct}%` }} />
            </div>
            <span className="tnum text-sm font-semibold">
              {Math.floor(s.limiter.remaining_estimated)} / {s.limiter.limit}
            </span>
          </div>
          <div className="mt-1 text-xs text-muted">
            {s.limiter.calls_total} calls · {s.limiter.throttled_total} throttled · {s.limiter.errors_total} errors · keeps {s.limiter.reserve} in reserve
            {s.limiter.waiting_for_credit && <b className="text-warn-ink"> · waiting for credits</b>}
          </div>
          {s.limiter.last_error ? (
            <div className="mt-1 text-xs text-crit-ink">{s.limiter.last_error}</div>
          ) : s.limiter.resolved_error ? (
            <div className="mt-1 text-xs text-muted">
              Last error{s.limiter.last_error_at ? ` at ${new Date(s.limiter.last_error_at * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}` : ""} - resolved, calls succeed again:{" "}
              {s.limiter.resolved_error}
            </div>
          ) : null}
        </Card>
      </div>

      {s.omsguru && <OmsConnectionCard o={s.omsguru} />}

      {s.live_lookup && (
        <Card className="p-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-sm font-semibold">Per-scan live lookups (since server start)</h2>
            <span className="text-xs text-muted">
              {s.live_lookup.enabled ? `on - max ${s.live_lookup.timeout_seconds}s per scan` : "off (LIVE_LOOKUP=false)"}
            </span>
          </div>
          <p className="mt-1 text-xs text-muted">
            Every scan asks OMSGuru for the order - by sub-order id or by order id, whichever finds that channel's shipments. If no API credit is free it is checked against the local copy instead, so scanning never waits.
          </p>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {(
              [
                ["fresh", "Live from OMSGuru"],
                ["not_in_oms", "Not in OMSGuru"],
                ["busy", "API busy - local copy"],
                ["timeout", "Slow - local copy"],
              ] as const
            ).map(([k, label]) => (
              <div key={k} className="rounded-lg bg-surface-2 px-3 py-2">
                <div className="text-xs text-ink-2">{label}</div>
                <div className="tnum text-lg font-semibold">{(s.live_lookup!.outcomes[k] ?? 0).toLocaleString("en-IN")}</div>
              </div>
            ))}
          </div>
          {!!s.live_lookup.methods?.length && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[520px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-2 pr-3 font-medium">Sales channel</th>
                    <th className="px-3 py-2 font-medium">Found by</th>
                    <th className="px-3 py-2 text-right font-medium">Sub-order id (found / missed)</th>
                    <th className="px-3 py-2 text-right font-medium">Order id (found / missed)</th>
                  </tr>
                </thead>
                <tbody className="tnum divide-y divide-line">
                  {s.live_lookup.methods.map((m) => (
                    <tr key={m.channel_id ?? "none"}>
                      <td className="py-2 pr-3">{m.name}</td>
                      <td className="px-3 py-2">{m.best === "sub" ? "Sub-order id" : m.best === "order" ? "Order id" : <span className="text-warn-ink">Not found yet</span>}</td>
                      <td className="px-3 py-2 text-right text-ink-2">{m.sub[0]} / {m.sub[1]}</td>
                      <td className="px-3 py-2 text-right text-ink-2">{m.order[0]} / {m.order[1]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      <CrossCheckCard c={s.crosscheck} exitPending={s.exit_check_pending ?? 0} onRun={() => void trigger("crosscheck")} />

      <BackupCard notify={notify} />

      <Card>
        <ul className="divide-y divide-line">
          {s.jobs.map((j) => (
            <li key={j.name} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
              {j.running ? (
                <RefreshCw className="size-4 animate-spin text-accent-ink" aria-label="Running" />
              ) : j.last_ok === false ? (
                <CircleAlert className="size-4 text-crit" aria-label="Last run failed" />
              ) : (
                <CheckCircle2 className={cx("size-4", j.last_ok ? "text-good" : "text-muted")} aria-label={j.last_ok ? "Last run OK" : "Not run yet"} />
              )}
              <div className="min-w-0 flex-1">
                <div className="font-medium">{JOB_LABEL[j.name] ?? j.name}</div>
                <div className="truncate text-xs text-muted">
                  {j.last_finished ? `last ${new Date(j.last_finished * 1000).toLocaleTimeString("en-IN")}` : "not run yet"}
                  {j.last_message && ` · ${j.last_message}`}
                </div>
              </div>
              <Button size="sm" onClick={() => void trigger(j.name)}>
                Run now
              </Button>
            </li>
          ))}
        </ul>
      </Card>

      <Card>
        <div className="border-b border-line px-4 py-3 text-sm font-semibold">Recent sync runs</div>
        {s.logs.length === 0 ? (
          <Empty>No runs yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <tbody className="divide-y divide-line">
                {s.logs.map((l, i) => (
                  <tr key={i}>
                    <td className="whitespace-nowrap px-4 py-2 text-ink-2">{fmtDateTime(l.started_at)}</td>
                    <td className="px-3 py-2">{JOB_LABEL[l.job] ?? l.job}</td>
                    <td className={cx("px-3 py-2", l.ok ? "text-good-ink" : "text-crit-ink")}>{l.ok ? "OK" : "Failed"}</td>
                    <td className="tnum px-3 py-2 text-right text-ink-2">{l.calls} calls</td>
                    <td className="tnum px-3 py-2 text-right text-ink-2">{l.records} rows</td>
                    <td className="px-3 py-2 text-xs text-muted">{l.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
