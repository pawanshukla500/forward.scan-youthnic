import {
  AlertOctagon,
  ArrowRight,
  CheckCircle2,
  Download,
  PackageCheck,
  RefreshCw,
  ScanLine,
  Timer,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, download, qs, type AwbCounts } from "../api";
import { AwbProgress } from "../components/AwbProgress";
import { ColumnChart } from "../components/ColumnChart";
import { Icon } from "../components/icons";
import {
  Button,
  ChannelDot,
  cx,
  Empty,
  IconButton,
  inputCls,
  LiveBadge,
  OutcomePill,
  SectionCard,
  Skeleton,
  SkeletonRows,
} from "../components/ui";
import { useLive, useLiveStatus, useThrottled } from "../live";

interface ScanChannel {
  id: number;
  name: string;
  marketplace: string;
  color: string;
  scanned: number;
  ok: number;
  warn: number;
  unverified: number;
  alerts: number;
  duplicate: number;
  wrong_channel: number;
  blocked: number;
  invalid: number;
  pending: number;
}

interface Dash {
  date: string;
  is_today: boolean;
  totals: Record<string, number>;
  channels: ScanChannel[];
  users: { user_id: number; name: string; scanned: number }[];
  hourly: number[];
  metrics?: {
    success_rate: number | null;
    flagged: number;
    /** "Not found" scans: flagged, not counted in scanned until OMSGuru has the order */
    not_found?: number;
    rejected: number;
    avg_scan_seconds: number | null;
    same_day_last_week: number;
    change_vs_last_week: number | null;
  };
  sync: null | {
    mode: string;
    invoices: { last_finished: number | null; last_ok: boolean | null; last_message: string } | null;
    limiter: { remaining_estimated: number; limit: number; throttled_total: number; waiting_for_credit: boolean; last_error: string };
  };
}

interface RecChannel extends AwbCounts {
  id: number | null;
  name: string;
  marketplace: string;
  color: string;
}

interface Rec {
  date: string;
  is_today: boolean;
  totals: AwbCounts;
  channels: RecChannel[];
}

interface EventRow {
  id: number;
  local: string;
  user: string;
  channel: string;
  tracking: string;
  outcome: string;
  message: string;
}

const n = (v: number | undefined | null) => (v ?? 0).toLocaleString("en-IN");

export default function Dashboard() {
  const live = useLiveStatus();
  const [range, setRange] = useState(7);
  const [date, setDate] = useState("");
  const [dash, setDash] = useState<Dash | null>(null);
  const [rec, setRec] = useState<Rec | null>(null);
  const [hist, setHist] = useState<{ days: { date: string; total: number }[] } | null>(null);
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setBusy(true);
    Promise.all([
      api<Dash>(`/api/dashboard${qs({ date })}`).then(setDash),
      api<Rec>(`/api/reconciliation${qs({ date })}`).then(setRec),
      api<{ days: { date: string; total: number }[] }>(`/api/history?days=${range}`).then(setHist),
      api<{ events: EventRow[] }>(`/api/events${qs({ date, outcome: "DUPLICATE,WRONG_CHANNEL,BLOCKED,INVALID", limit: 8 })}`).then((r) => setEvents(r.events)),
    ])
      .then(() => {
        setUpdatedAt(Date.now());
        setErr("");
      })
      .catch((e) => setErr(e.message))
      .finally(() => setBusy(false));
  }, [date, range]);

  useEffect(load, [load]);
  const refreshSoon = useThrottled(load, 1500);
  useLive((event) => {
    if (!["scan", "scan_rejected", "scan_updated", "scan_voided", "sync"].includes(event)) return;
    refreshSoon();
  });

  const hourly = useMemo(
    () => (dash?.hourly ?? []).map((v, h) => ({ key: String(h), label: String(h).padStart(2, "0"), tip: `${String(h).padStart(2, "0")}:00 - ${String(h).padStart(2, "0")}:59`, value: v })),
    [dash],
  );
  const daily = useMemo(
    () =>
      (hist?.days ?? []).map((d) => {
        const dt = new Date(d.date + "T00:00:00");
        return { key: d.date, label: range <= 7 ? dt.toLocaleDateString("en-IN", { weekday: "short" }) : dt.toLocaleDateString("en-IN", { day: "2-digit", month: "short" }), tip: dt.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" }), value: d.total };
      }),
    [hist, range],
  );

  const t = dash?.totals;
  const a = rec?.totals;
  const dayWord = dash?.is_today !== false ? "today" : `on ${dash?.date}`;
  const m = dash?.metrics;
  const scanByChannel = new Map((dash?.channels ?? []).map((c) => [c.id, c]));
  const readiness = (rec?.channels ?? []).filter((c) => c.id !== null || c.generated > 0);
  const pend = (c: { pending: number; overdue: number; pending_all?: number }) => c.pending_all ?? c.pending + c.overdue;
  const pendingTop = [...(rec?.channels ?? [])].filter((c) => pend(c) > 0).sort((x, y) => pend(y) - pend(x)).slice(0, 4);
  const lastSync = dash?.sync?.invoices?.last_finished ? new Date(dash.sync.invoices.last_finished * 1000) : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3" role="group" aria-label="Dashboard controls">
        <div className="min-w-0">
          <span className="eyebrow">{dash ? new Date(dash.date + "T00:00:00").toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "Dispatch"}</span>
          <p className="mt-0.5 text-sm text-muted">AWBs generated in OMSGuru vs shipments scanned. Updates live as stations scan.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <LiveBadge updatedAt={updatedAt} connected={live} />
          <input type="date" className={`${inputCls} w-auto`} value={date || dash?.date || ""} onChange={(e) => setDate(e.target.value)} aria-label="Dispatch date" />
          <IconButton label="Refresh" onClick={load} className="border border-line-strong bg-surface">
            <RefreshCw className={cx("size-4", busy && "animate-spin")} aria-hidden />
          </IconButton>
          <Button onClick={() => void download(`/api/scans/export.xlsx${qs({ date_from: dash?.date })}`).catch((e) => alert(e.message))} disabled={!dash}>
            <Download className="size-4" aria-hidden /> Excel
          </Button>
        </div>
      </div>

      {err && (
        <p className="flex items-center gap-2 rounded-lg bg-crit-wash px-4 py-3 text-sm text-crit-ink" role="alert">
          <AlertOctagon className="size-4" aria-hidden /> Could not load the dashboard: {err}
        </p>
      )}
      {dash?.sync && <SyncBanner sync={dash.sync} lastSync={lastSync} />}

      <div className="dashboard">
        <div className="metric-grid">
          {!dash
            ? [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[118px] w-full" />)
            : (
              [
                ["Successful scans", n(t?.scanned), m?.change_vs_last_week == null ? "—" : `${m.change_vs_last_week > 0 ? "+" : ""}${m.change_vs_last_week}%`, "scan", false, true],
                ["Success rate", m?.success_rate == null ? "-" : `${m.success_rate}%`, `${n(t?.ok)} verified`, "check", false, false],
                ["Flagged orders", n(m?.flagged), `${m?.not_found ? `${n(m.not_found)} not found (not counted) · ` : ""}${n(m?.rejected)} stopped`, "alert", true, false],
                ["Avg. scan time", m?.avg_scan_seconds == null ? "-" : `${m.avg_scan_seconds < 10 ? m.avg_scan_seconds.toFixed(1) : Math.round(m.avg_scan_seconds)} sec`, "per operator", "box", false, false],
              ] as const
            ).map(([label, value, delta, icon, flagged, week]) => (
              <div className="metric card" key={label}>
                <div className="metric-icon">
                  <Icon name={icon} />
                </div>
                <span>{label}</span>
                <b>{value}</b>
                <small className={flagged ? "" : "positive"}>
                  {delta}
                  {week && <em> vs last week</em>}
                </small>
              </div>
            ))}
        </div>
        <div className="dashboard-grid">
          <section className="card chart-card">
            <div className="card-head">
              <div>
                <span className="eyebrow">Scan volume</span>
                <h2>Daily forward scans</h2>
              </div>
              <select aria-label="Date range" value={range} onChange={(e) => setRange(Number(e.target.value))}>
                <option value={7}>Last 7 days</option>
                <option value={30}>Last 30 days</option>
              </select>
            </div>
            {hist ? (
              <div className="chart" role="img" aria-label={`Shipments scanned per day, last ${range} days`}>
                {daily.map((d) => {
                  const max = Math.max(1, ...daily.map((x) => x.value));
                  return (
                    <div className="bar-wrap" key={d.key}>
                      <span>{d.value.toLocaleString("en-IN")}</span>
                      <div className="bar" style={{ height: `${Math.max(4, Math.round((d.value / max) * 100))}%` }} />
                      <small>{d.label}</small>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="p-6">
                <SkeletonRows rows={4} />
              </div>
            )}
          </section>
          <section className="card">
            <div className="card-head">
              <div>
                <span className="eyebrow">Live operations</span>
                <h2>Scan quality</h2>
              </div>
            </div>
            {dash ? <QualityDonut ok={t?.ok ?? 0} flagged={m?.flagged ?? 0} stopped={m?.rejected ?? 0} rate={m?.success_rate ?? null} /> : <div className="p-6"><SkeletonRows rows={4} /></div>}
          </section>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
        {/* channel readiness */}
        <SectionCard
          eyebrow="AWB reconciliation"
          title="Channel readiness"
          description={
            a
              ? `${n(a.synced ?? a.generated - a.cancelled)} synced orders · ${n(a.scanned)} scanned · ${n(pend(a))} pending${a.cancelled ? ` · ${n(a.cancelled)} cancelled (not counted)` : ""}`
              : `AWBs generated ${dayWord} and how many are scanned`
          }
          actions={
            <Link to="/pending" className="inline-flex min-h-11 items-center gap-1 rounded-lg px-2 text-sm font-medium text-accent-ink hover:underline">
              Reconciliation <ArrowRight className="size-4" aria-hidden />
            </Link>
          }
        >
          {!rec ? (
            <SkeletonRows rows={5} />
          ) : readiness.length === 0 ? (
            <Empty title="No AWBs yet" icon={PackageCheck}>
              Nothing has been invoiced in OMSGuru for this day yet.
            </Empty>
          ) : (
            <ul className="divide-y divide-line">
              {readiness.map((c) => {
                const sc = c.id ? scanByChannel.get(c.id) : undefined;
                return (
                  <li key={String(c.id)} className="grid grid-cols-1 items-center gap-x-6 gap-y-2 px-4 py-3.5 sm:px-5 md:grid-cols-[minmax(220px,1.3fr)_minmax(200px,1.3fr)_auto]">
                    <div className="flex min-w-0 items-center gap-3">
                      <ChannelDot color={c.color} size={10} />
                      <div className="min-w-0">
                        <div className="truncate font-semibold" title={c.name}>{c.name}</div>
                        <div className="truncate text-sm text-muted">
                          {c.marketplace}
                          {sc && sc.alerts > 0 && <span className="ml-2 font-medium text-crit-ink">· {sc.alerts} alert{sc.alerts > 1 ? "s" : ""}</span>}
                        </div>
                      </div>
                    </div>
                    <AwbProgress c={c} compact title="Synced orders" />
                    <div className="flex items-center gap-2 md:justify-end">
                      {pend(c) > 0 && (
                        <Link to={`/pending${qs({ date, bucket: "pending", channel_id: c.id ?? "" })}`} className="tnum inline-flex h-9 items-center rounded-full bg-warn-wash px-3 text-xs font-semibold text-warn-ink hover:underline">
                          {n(pend(c))} pending
                        </Link>
                      )}
                      {c.id && (
                        <Link
                          to={`/scan/${c.id}`}
                          className="ease-ui inline-flex h-9 items-center gap-1.5 rounded-lg border border-line-strong px-3 text-sm font-medium hover:bg-surface-2"
                          aria-label={`Scan ${c.name}`}
                        >
                          <ScanLine className="size-4" aria-hidden /> Scan
                        </Link>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </SectionCard>

        {/* needs attention */}
        <SectionCard eyebrow="Exceptions" title="Needs attention" description="Pending AWBs and stopped scans, newest first">
          {!rec || !events ? (
            <SkeletonRows rows={4} />
          ) : pendingTop.length === 0 && events.length === 0 && !t?.alerts ? (
            <Empty title="All clear" icon={CheckCircle2}>
              Nothing pending and no rejected scans {dayWord}.
            </Empty>
          ) : (
            <div className="divide-y divide-line">
              {pendingTop.length > 0 && (
                <div className="space-y-1 px-4 py-3 sm:px-5">
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted">Pending AWBs</div>
                  {pendingTop.map((c) => (
                    <Link
                      key={String(c.id)}
                      to={`/pending${qs({ date, bucket: "pending", channel_id: c.id ?? "" })}`}
                      className="ease-ui flex min-h-11 items-center justify-between gap-3 rounded-lg px-2 hover:bg-surface-2"
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <ChannelDot color={c.color} size={8} />
                        <span className="truncate text-sm font-medium">{c.name}</span>
                      </span>
                      <span className="tnum inline-flex items-center gap-1 text-sm font-semibold text-warn-ink">
                        <Timer className="size-3.5" aria-hidden /> {n(pend(c))}
                      </span>
                    </Link>
                  ))}
                </div>
              )}
              <div className="px-4 py-3 sm:px-5">
                <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Recent stopped scans</div>
                {events.length === 0 ? (
                  <p className="py-2 text-sm text-muted">None {dayWord}.</p>
                ) : (
                  <ul className="space-y-3">
                    {events.map((e) => (
                      <li key={e.id} className="text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          <OutcomePill outcome={e.outcome} />
                          <span className="font-mono text-[13px]">{e.tracking}</span>
                        </div>
                        <div className="mt-0.5 truncate text-xs text-muted" title={e.message}>
                          {e.local} · {e.user} · {e.channel}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <SectionCard eyebrow="Rhythm" title="Scans per hour" description={`${dash?.date ?? ""} · local time`} bodyClass="p-4 sm:p-5">
          {dash ? <ColumnChart data={hourly} labelEvery={3} caption={`Shipments scanned per hour on ${dash.date}`} /> : <SkeletonRows rows={4} />}
        </SectionCard>
        <SectionCard eyebrow="Operators" title="Scans by user" description={dayWord === "today" ? "Today" : dash?.date}>
          {!dash ? (
            <SkeletonRows rows={4} />
          ) : dash.users.length === 0 ? (
            <Empty title="No scans yet" icon={ScanLine} />
          ) : (
            <ol className="divide-y divide-line">
              {dash.users.map((u, i) => {
                const max = dash.users[0]?.scanned || 1;
                return (
                  <li key={u.user_id} className="flex items-center gap-3 px-4 py-2.5 sm:px-5">
                    <span className="tnum w-5 text-right text-sm text-muted">{i + 1}</span>
                    <span className="w-32 truncate text-sm font-medium">{u.name}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2" aria-hidden>
                      <div className="h-full rounded-full bg-accent" style={{ width: `${(u.scanned / max) * 100}%` }} />
                    </div>
                    <span className="tnum w-14 text-right text-sm font-semibold">{n(u.scanned)}</span>
                  </li>
                );
              })}
            </ol>
          )}
        </SectionCard>
      </div>

      <div>
        <SectionCard eyebrow="By sales channel" title="Scan quality by channel" description="Accepted scans and the wrong scans the system stopped">
          {!dash ? (
            <SkeletonRows rows={5} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs font-semibold uppercase tracking-wider text-muted">
                    <th scope="col" className="px-4 py-2.5 sm:px-5">Sales channel</th>
                    <th scope="col" className="px-3 py-2.5 text-right" title="Successful scans only (verified + check). Not found scans are not counted until OMSGuru has the order.">Scanned</th>
                    <th scope="col" className="px-3 py-2.5 text-right">Verified</th>
                    <th scope="col" className="px-3 py-2.5 text-right">Check</th>
                    <th scope="col" className="px-3 py-2.5 text-right" title="Not found attempts - NOT saved, NOT counted as scanned">Not found</th>
                    <th scope="col" className="px-3 py-2.5 text-right">Duplicate</th>
                    <th scope="col" className="px-3 py-2.5 text-right">Wrong bag</th>
                    <th scope="col" className="px-4 py-2.5 text-right sm:px-5">Blocked</th>
                  </tr>
                </thead>
                <tbody className="tnum divide-y divide-line">
                  {dash.channels.map((c) => (
                    <tr key={c.id} className="ease-ui hover:bg-surface-2/70">
                      <th scope="row" className="px-4 py-2.5 text-left font-medium sm:px-5">
                        <span className="flex items-center gap-2">
                          <ChannelDot color={c.color} size={8} />
                          <span className="truncate">{c.name}</span>
                        </span>
                      </th>
                      <td className="px-3 text-right font-semibold">{n(c.scanned)}</td>
                      <td className="px-3 text-right text-good-ink">{n(c.ok)}</td>
                      <td className={cx("px-3 text-right", c.warn ? "text-warn-ink" : "text-muted")}>{n(c.warn)}</td>
                      <td className={cx("px-3 text-right", c.unverified ? "font-semibold text-nf-ink" : "text-muted")}>{n(c.unverified)}</td>
                      <td className={cx("px-3 text-right", c.duplicate ? "text-ink" : "text-muted")}>{n(c.duplicate)}</td>
                      <td className={cx("px-3 text-right", c.wrong_channel ? "text-ink" : "text-muted")}>{n(c.wrong_channel)}</td>
                      <td className={cx("px-4 text-right sm:px-5", c.blocked ? "text-crit-ink" : "text-muted")}>{n(c.blocked)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
}

function QualityDonut({ ok, flagged, stopped, rate }: { ok: number; flagged: number; stopped: number; rate: number | null }) {
  const total = ok + flagged + stopped;
  const p1 = total ? (100 * ok) / total : 0;
  const p2 = total ? (100 * (ok + flagged)) / total : 0;
  const ring = total
    ? `conic-gradient(var(--brand) 0 ${p1}%, #d99b38 ${p1}% ${p2}%, var(--border) ${p2}% 100%)`
    : "conic-gradient(var(--border) 0 100%)";
  return (
    <div className="donut-row">
      <div className="donut" style={{ background: ring }} role="img" aria-label={`Scan quality: ${ok} verified, ${flagged} flagged, ${stopped} stopped`}>
        <span>
          {rate === null ? "-" : `${rate}%`}
          <small>Successful</small>
        </span>
      </div>
      <div className="legend">
        <span>
          <i className="green" />
          Successful <b>{ok.toLocaleString("en-IN")}</b>
        </span>
        <span>
          <i className="amber" />
          Flagged <b>{flagged.toLocaleString("en-IN")}</b>
        </span>
        <span>
          <i className="gray" />
          Failed <b>{stopped.toLocaleString("en-IN")}</b>
        </span>
      </div>
    </div>
  );
}

function SyncBanner({ sync, lastSync }: { sync: NonNullable<Dash["sync"]>; lastSync: Date | null }) {
  const starving = sync.limiter.waiting_for_credit || (sync.limiter.throttled_total > 0 && sync.limiter.remaining_estimated < 2);
  const stale = !lastSync || Date.now() - lastSync.getTime() > 10 * 60 * 1000;
  if (sync.mode === "mock")
    return (
      <div className="rounded-lg border border-line bg-info-wash px-4 py-3 text-sm text-ink-2" role="note">
        <b className="text-ink">Demo mode</b> - sample orders (OMSGURU_USE_MOCK=true). Nothing is read from or written to OMSGuru.
      </div>
    );
  if (!starving && !stale) return null;
  return (
    <div className="flex items-start gap-3 rounded-lg bg-warn-wash px-4 py-3 text-sm text-warn-ink" role="status">
      <AlertOctagon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div>
        <b>OMSGuru sync is delayed.</b>{" "}
        {starving
          ? "The OMSGuru API limit (60 calls / 5 min) is in use by another integration. Scans are still saved and verify when credits free up."
          : `Last order sync ${lastSync ? lastSync.toLocaleTimeString("en-IN") : "never"}.`}{" "}
        {sync.limiter.last_error && <span className="opacity-80">({sync.limiter.last_error})</span>}
      </div>
    </div>
  );
}
