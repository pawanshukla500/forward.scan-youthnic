import { ChevronLeft, ChevronRight, Download, FileSpreadsheet, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api, download, fmtDateTime, qs, todayISO, type Channel, type Order, type Scan } from "../api";
import { isSupervisor, useAuth } from "../App";
import { initials } from "../components/Shell";
import { Button, cx, Empty, FlagChips, OutcomePill, ResultPill, Skeleton, SkeletonRows, Toast } from "../components/ui";
import { useLive, useThrottled } from "../live";
import ChannelSummary from "./ChannelSummary";

/* ---- types --------------------------------------------------------------------------------- */

interface EventRow {
  id: number;
  local: string;
  user: string;
  station: string;
  channel: string;
  tracking: string;
  outcome: string;
  message: string;
}
interface PendingRow {
  awb: string;
  bucket: string;
  awb_generated_local: string;
  age_days: number | null;
  sla_breached: boolean;
  order: Order | null;
}
interface SkuRow {
  sku: string;
  scanned: number;
  pending: number;
  top_channel: string;
  top_share: number | null;
}
interface OperatorRow {
  user_id: number;
  name: string;
  scans: number;
  success_rate: number | null;
  flagged: number;
  rejected: number;
  avg_scan_seconds: number | null;
  stations: string[];
  first: string;
  last: string;
}
interface DashChannel {
  id: number;
  name: string;
  marketplace: string;
  color: string;
  scanned: number;
  ok: number;
  pending: number;
}

type View = "scans" | "pending" | "sku" | "operators" | "audit" | "summary";
const VIEWS: [View, string][] = [
  ["scans", "Daily scans"],
  ["pending", "Pending"],
  ["sku", "SKU summary"],
  ["operators", "Operators"],
  ["audit", "Audit log"],
  ["summary", "Channel summary"],
];

type Range = "today" | "yesterday" | "week" | "custom";

function shift(iso: string, days: number) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const selCls = "h-10 min-w-0 cursor-pointer rounded-lg border border-line-strong bg-surface px-3 text-sm font-semibold text-ink";

function Pick({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cx("grid min-w-0 gap-1 text-xs font-semibold text-muted", className)}>
      {label}
      {children}
    </label>
  );
}

/* ---- page ---------------------------------------------------------------------------------- */

export default function Scans() {
  const { user } = useAuth();
  const sup = isSupervisor(user);
  const [sp] = useSearchParams();
  const [view, setView] = useState<View>("scans");
  const [range, setRange] = useState<Range>("today");
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [channelId, setChannelId] = useState(sp.get("channel_id") || "");
  const [courier, setCourier] = useState("");
  const [operator, setOperator] = useState("");
  const [status, setStatus] = useState(sp.get("result") || (sp.get("alerts_only") ? "ALERTS" : ""));
  const [q, setQ] = useState(sp.get("q") || "");
  const [pendingBucket, setPendingBucket] = useState<"pending" | "overdue">("pending");
  const [page, setPage] = useState(1);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [filters, setFilters] = useState<{ couriers: string[]; operators: { id: number; name: string }[] }>({ couriers: [], operators: [] });
  const [summary, setSummary] = useState<DashChannel[] | null>(null);
  const [scans, setScans] = useState<{ total: number; scans: Scan[] } | null>(null);
  const [pending, setPending] = useState<{ total: number; rows: PendingRow[] } | null>(null);
  const [skus, setSkus] = useState<SkuRow[] | null>(null);
  const [ops, setOps] = useState<OperatorRow[] | null>(null);
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [updated, setUpdated] = useState<number | null>(null);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const today = todayISO();
  const [dateFrom, dateTo] = useMemo((): [string, string] => {
    if (range === "yesterday") return [shift(today, -1), shift(today, -1)];
    if (range === "week") return [shift(today, -6), today];
    if (range === "custom") return [custom.from || today, custom.to || custom.from || today];
    return [today, today];
  }, [range, custom, today]);
  const rangeLabel = range === "today" ? "Today" : range === "yesterday" ? "Yesterday" : range === "week" ? "Last 7 days" : `${dateFrom} to ${dateTo}`;

  const scanFilter = {
    date_from: dateFrom,
    date_to: dateTo,
    channel_id: channelId,
    courier,
    user_id: operator,
    result: status === "ALERTS" ? "" : status,
    alerts_only: status === "ALERTS",
    q,
  };

  useEffect(() => {
    api<{ channels: Channel[] }>("/api/channels").then((r) => setChannels(r.channels));
  }, []);
  useEffect(() => {
    api<typeof filters>(`/api/reports/filters${qs({ date_from: dateFrom, date_to: dateTo })}`).then(setFilters).catch(() => {});
    api<{ channels: DashChannel[] }>(`/api/dashboard${qs({ date: dateTo })}`).then((d) => setSummary(d.channels)).catch(() => setSummary([]));
  }, [dateFrom, dateTo]);

  const load = useCallback(() => {
    const done = () => setUpdated(Date.now());
    if (view === "scans") api<{ total: number; scans: Scan[] }>(`/api/scans${qs({ ...scanFilter, page, page_size: 50 })}`).then(setScans).then(done);
    else if (view === "pending")
      api<{ total: number; rows: PendingRow[] }>(`/api/reconciliation/list${qs({ date: dateTo, bucket: pendingBucket, channel_id: channelId, page, page_size: 100 })}`)
        .then(setPending)
        .then(done);
    else if (view === "sku") api<{ rows: SkuRow[] }>(`/api/reports/sku-summary${qs({ date_from: dateFrom, date_to: dateTo, channel_id: channelId })}`).then((r) => setSkus(r.rows)).then(done);
    else if (view === "operators")
      api<{ rows: OperatorRow[] }>(`/api/reports/operators${qs({ date_from: dateFrom, date_to: dateTo, channel_id: channelId })}`).then((r) => setOps(r.rows)).then(done);
    else if (view === "audit") api<{ events: EventRow[] }>(`/api/events${qs({ date: dateTo, channel_id: channelId, limit: 500 })}`).then((r) => setEvents(r.events)).then(done);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, page, pendingBucket, JSON.stringify(scanFilter)]);

  useEffect(load, [load]);
  const refreshSoon = useThrottled(load, 1200);
  useLive((event) => {
    if (page !== 1 || !["scan", "scan_rejected", "scan_updated", "scan_voided", "sync"].includes(event)) return;
    refreshSoon();
  });

  function reset<T>(fn: (v: T) => void) {
    return (v: T) => {
      fn(v);
      setPage(1);
    };
  }

  async function voidScan(s: Scan) {
    const reason = window.prompt(`Remove scan ${s.tracking}? Enter a reason:`);
    if (reason === null) return;
    try {
      await api(`/api/scans/${s.id}${qs({ reason })}`, { method: "DELETE" });
      setToast({ kind: "ok", text: `Scan ${s.tracking} removed` });
      load();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  }

  function localCsv() {
    const esc = (v: unknown) => {
      const t = String(v ?? "");
      return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
    };
    let head: string[] = [];
    let body: unknown[][] = [];
    if (view === "sku" && skus) {
      head = ["SKU", "Units scanned", "Units pending", "Top marketplace", "Share %"];
      body = skus.map((r) => [r.sku, r.scanned, r.pending, r.top_channel, r.top_share ?? ""]);
    } else if (view === "operators" && ops) {
      head = ["Operator", "Scans", "Avg scan seconds", "Success rate %", "Flagged", "Stopped", "Stations", "First scan", "Last scan"];
      body = ops.map((r) => [r.name, r.scans, r.avg_scan_seconds ?? "", r.success_rate ?? "", r.flagged, r.rejected, r.stations.join(" / "), fmtDateTime(r.first), fmtDateTime(r.last)]);
    } else if (view === "audit" && events) {
      head = ["Time", "Operator", "Scanned", "Outcome", "Details", "Marketplace", "Station"];
      body = events.map((e) => [e.local, e.user, e.tracking, e.outcome, e.message, e.channel, e.station]);
    }
    const text = "\ufeff" + [head, ...body].map((r) => r.map(esc).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${view}_${dateFrom}${dateFrom !== dateTo ? `_to_${dateTo}` : ""}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  async function exportFile(kind: "xlsx" | "csv") {
    if (kind === "csv" && (view === "sku" || view === "operators" || view === "audit")) return localCsv();
    try {
      if (view === "pending") await download(`/api/reconciliation/export.xlsx${qs({ date: dateTo, bucket: pendingBucket, channel_id: channelId })}`);
      else if (view === "summary") await download(`/api/reports/channel-summary.xlsx${qs({ group: "month" })}`);
      else await download(`/api/scans/export.${kind}${qs(scanFilter)}`);
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  }

  const colorOf = (id: number) => channels.find((c) => c.id === id)?.color ?? "#5f6b65";
  const active = (summary ?? []).filter((c) => c.scanned > 0 || c.pending > 0);
  const rows =
    view === "scans" ? scans?.scans.length : view === "pending" ? pending?.rows.length : view === "sku" ? skus?.length : view === "operators" ? ops?.length : view === "audit" ? events?.length : undefined;
  const total = view === "scans" ? scans?.total : view === "pending" ? pending?.total : rows;
  const pageSize = view === "pending" ? 100 : 50;
  const pages = total ? Math.max(1, Math.ceil(total / pageSize)) : 1;
  const singleDayNote = (view === "pending" || view === "audit") && dateFrom !== dateTo ? `Showing ${dateTo} (this view is one day at a time)` : "";

  return (
    <div className="reports-page">
      {/* per-channel summary cards */}
      <div className="report-summary-grid">
        {!summary ? (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[92px] w-full rounded-[14px]" />)
        ) : active.length === 0 ? (
          <div className="card col-span-full px-5 py-4 text-sm text-muted">No scans or pending AWBs for {rangeLabel.toLowerCase()}.</div>
        ) : (
          active.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => {
                setChannelId(String(c.id));
                setPage(1);
              }}
              aria-pressed={channelId === String(c.id)}
              className={cx("card report-summary", channelId === String(c.id) && "border-accent")}
            >
              <span className="market-logo small" style={{ background: c.color }} aria-hidden>
                {(c.marketplace || c.name).slice(0, 1).toUpperCase()}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium text-muted" title={c.name}>
                  {c.name}
                </span>
                <span className="tnum block text-xl font-bold">{c.scanned.toLocaleString("en-IN")}</span>
                <span className="block text-xs text-muted">scanned {dateTo === today ? "today" : dateTo}</span>
              </span>
              <span className="text-right">
                <span className="block text-xs font-medium text-muted">Pending</span>
                <span className={cx("tnum block text-xl font-bold", c.pending ? "text-warn-ink" : "text-ink")}>{c.pending.toLocaleString("en-IN")}</span>
                <span className="block text-xs text-muted">{c.scanned ? `${Math.round((100 * c.ok) / c.scanned)}% verified` : "-"}</span>
              </span>
            </button>
          ))
        )}
      </div>

      {/* the sheet */}
      <section className="card sheet-card" aria-labelledby="sheet-title">
        <div className="sheet-toolbar">
          <div className="min-w-0">
            <span className="eyebrow">Spreadsheet preview</span>
            <h2 id="sheet-title" className="mt-1 text-[17px] font-bold">
              Marketplace scan report
            </h2>
            <p className="text-sm text-muted">Review orders, SKU quantities and scan status before exporting.</p>
          </div>
          <div className="sheet-actions">
            <Pick label="Date">
              <select className={selCls} value={range} onChange={(e) => reset(setRange)(e.target.value as Range)}>
                <option value="today">Today</option>
                <option value="yesterday">Yesterday</option>
                <option value="week">Last 7 days</option>
                <option value="custom">Custom range</option>
              </select>
            </Pick>
            {range === "custom" && (
              <>
                <Pick label="From">
                  <input type="date" className={selCls} value={custom.from} max={today} onChange={(e) => reset(setCustom)({ ...custom, from: e.target.value })} />
                </Pick>
                <Pick label="To">
                  <input type="date" className={selCls} value={custom.to} max={today} onChange={(e) => reset(setCustom)({ ...custom, to: e.target.value })} />
                </Pick>
              </>
            )}
            <Pick label="Marketplace">
              <select className={cx(selCls, "max-w-[230px]")} value={channelId} onChange={(e) => reset(setChannelId)(e.target.value)}>
                <option value="">All marketplaces</option>
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </Pick>
            {(view === "scans" || view === "pending" || view === "summary") && (
              <Button onClick={() => void exportFile("xlsx")} className="secondary">
                <FileSpreadsheet className="size-4" aria-hidden /> Excel
              </Button>
            )}
            {(view === "scans" || view === "sku" || view === "operators" || view === "audit") && (
              <Button variant="primary" onClick={() => void exportFile("csv")} className="primary">
                <Download className="size-4" aria-hidden /> CSV
              </Button>
            )}
          </div>
        </div>

        {view === "scans" && (
          <div className="advanced-filters">
            <Pick label="Carrier">
              <select className={selCls} value={courier} onChange={(e) => reset(setCourier)(e.target.value)}>
                <option value="">All carriers</option>
                {filters.couriers.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </Pick>
            <Pick label="Operator">
              <select className={selCls} value={operator} onChange={(e) => reset(setOperator)(e.target.value)}>
                <option value="">All operators</option>
                {filters.operators.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </Pick>
            <Pick label="Status">
              <select className={selCls} value={status} onChange={(e) => reset(setStatus)(e.target.value)}>
                <option value="">All statuses</option>
                <option value="OK">Verified</option>
                <option value="FLAGGED">Flagged / needs review</option>
                <option value="WARN">Accepted - check</option>
                <option value="UNVERIFIED">Not found (flagged, not counted)</option>
                <option value="ALERTS">Alert after scan</option>
              </select>
            </Pick>
            <Pick label="Search AWB / order / invoice">
              <span className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
                <input className={cx(selCls, "w-full cursor-text pl-9 font-normal")} value={q} onChange={(e) => reset(setQ)(e.target.value)} placeholder="Type to search" />
              </span>
            </Pick>
          </div>
        )}
        {view === "pending" && (
          <div className="flex flex-wrap items-end gap-3 border-t border-line bg-surface-2 px-4 py-3 sm:px-6">
            <Pick label="Show">
              <select className={selCls} value={pendingBucket} onChange={(e) => reset(setPendingBucket)(e.target.value as "pending" | "overdue")}>
                <option value="pending">AWB generated that day, not scanned</option>
                <option value="overdue">Overdue - AWB from an earlier day</option>
              </select>
            </Pick>
          </div>
        )}

        <div className="sheet-formula" aria-hidden>
          <span className="font-bold italic">fx</span>
          <b className="text-ink-2">A1:{view === "scans" ? "I" : view === "pending" ? "H" : "F"}{(rows ?? 0) + 1}</b>
          <i className="truncate font-sans not-italic">
            {VIEWS.find(([k]) => k === view)?.[1]} · {rangeLabel}
            {channelId ? ` · ${channels.find((c) => String(c.id) === channelId)?.name ?? ""}` : ""}
            {singleDayNote ? ` · ${singleDayNote}` : ""}
          </i>
        </div>

        <div className="min-w-0">
          {view === "scans" && <ScanSheet data={scans} colorOf={colorOf} sup={sup} onVoid={(s) => void voidScan(s)} offset={(page - 1) * 50} />}
          {view === "pending" && <PendingSheet data={pending} colorOf={colorOf} offset={(page - 1) * 100} />}
          {view === "sku" && <SkuSheet rows={skus} />}
          {view === "operators" && <OperatorSheet rows={ops} />}
          {view === "audit" && <AuditSheet rows={events} />}
          {view === "summary" && (
            <div className="p-4 sm:p-6">
              <ChannelSummary />
            </div>
          )}
        </div>

        <div className="sheet-footer">
          <div role="tablist" aria-label="Report view">
            {VIEWS.map(([k, label]) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={view === k}
                onClick={() => {
                  setView(k);
                  setPage(1);
                }}
                className={cx("sheet-tab", view === k && "active")}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3 px-2 py-2 text-xs text-muted">
            {view !== "summary" && (
              <span className="tnum">
                {(total ?? 0).toLocaleString("en-IN")} rows · Updated {updated ? new Date(updated).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "-"}
              </span>
            )}
            {(view === "scans" || view === "pending") && pages > 1 && (
              <span className="flex items-center gap-1">
                <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} aria-label="Previous page">
                  <ChevronLeft className="size-4" aria-hidden />
                </Button>
                <span className="tnum">
                  {page} / {pages}
                </span>
                <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => setPage((p) => p + 1)} aria-label="Next page">
                  <ChevronRight className="size-4" aria-hidden />
                </Button>
              </span>
            )}
          </div>
        </div>
      </section>

      {toast && (
        <Toast kind={toast.kind} onClose={() => setToast(null)}>
          {toast.text}
        </Toast>
      )}
    </div>
  );
}

/* ---- sheets -------------------------------------------------------------------------------- */

const th = "";
const td = "";
const rn = "row-number";

function Sheet({ minW, head, children }: { minW: number; head: string[]; children: ReactNode }) {
  return (
    <div className="sheet-scroll" style={{ maxHeight: 640 }}>
      <table className="data-sheet" style={{ minWidth: minW }}>
        <thead className="sticky top-0 z-[1]">
          <tr>
            <th className={rn} aria-label="Row" />
            {head.map((h) => (
              <th key={h} scope="col" className={th}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function MarketChip({ name, color }: { name: string; color: string }) {
  return (
    <span className="inline-flex max-w-[200px] items-center gap-1.5 rounded-full bg-surface-2 px-2 py-0.5 text-xs font-semibold">
      <span className="size-2 shrink-0 rounded-full" style={{ background: color }} aria-hidden />
      <span className="truncate">{name}</span>
    </span>
  );
}

function ScanSheet({
  data,
  colorOf,
  sup,
  onVoid,
  offset,
}: {
  data: { total: number; scans: Scan[] } | null;
  colorOf: (id: number) => string;
  sup: boolean;
  onVoid: (s: Scan) => void;
  offset: number;
}) {
  if (!data) return <SkeletonRows />;
  if (data.scans.length === 0) return <Empty title="No shipments match these filters" />;
  return (
    <Sheet minW={1080} head={["Order ID", "Tracking ID", "Marketplace", "SKUs", "Qty", "Status", "Operator", "Scan time", ...(sup ? [""] : [])]}>
      {data.scans.map((s, i) => {
        const skus = s.order?.items.map((it) => `${it.sku} x${it.qty}`).join(", ") ?? "";
        return (
          <tr key={s.id} className={cx("align-top hover:bg-surface-2/60", s.alert && "bg-crit-wash/40")}>
            <td className={rn}>{offset + i + 1}</td>
            <td className={cx(td, "font-mono text-xs")}>{s.order?.channel_order_id ?? "-"}</td>
            <td className={cx(td, "font-mono text-[13px] font-semibold")}>{s.tracking}</td>
            <td className={td}>
              <MarketChip name={s.channel_name} color={colorOf(s.channel_id)} />
            </td>
            <td className={cx(td, "max-w-[220px]")}>
              <span className="tnum">{s.order ? new Set(s.order.items.map((x) => x.sku)).size : "-"}</span>
              {skus && (
                <span className="block truncate text-xs text-muted" title={skus}>
                  {skus}
                </span>
              )}
            </td>
            <td className={cx(td, "tnum text-right")}>{s.order?.total_qty ?? "-"}</td>
            <td className={td}>
              <div className="flex flex-col items-start gap-1">
                <ResultPill result={s.result} alert={s.alert} />
                <FlagChips flags={s.flags} />
                {(s.alert || s.flags.includes("FLAGGED")) && <span className="text-xs font-medium text-crit-ink">{s.alert || s.message}</span>}
              </div>
            </td>
            <td className={cx(td, "text-ink-2")}>
              {s.user}
              {s.station && <span className="block text-xs text-muted">{s.station}</span>}
            </td>
            <td className={cx(td, "whitespace-nowrap text-ink-2")}>{s.scanned_at_local}</td>
            {sup && (
              <td className={td}>
                <button
                  type="button"
                  onClick={() => onVoid(s)}
                  className="grid size-9 cursor-pointer place-items-center rounded-lg text-muted hover:bg-crit-wash hover:text-crit-ink"
                  title="Remove scan"
                  aria-label={`Remove scan ${s.tracking}`}
                >
                  <Trash2 className="size-4" aria-hidden />
                </button>
              </td>
            )}
          </tr>
        );
      })}
    </Sheet>
  );
}

function PendingSheet({ data, colorOf, offset }: { data: { total: number; rows: PendingRow[] } | null; colorOf: (id: number) => string; offset: number }) {
  if (!data) return <SkeletonRows />;
  if (data.rows.length === 0) return <Empty title="Nothing pending - every AWB is scanned" />;
  return (
    <Sheet minW={980} head={["Tracking ID", "Order ID", "Marketplace", "SKUs", "Qty", "Courier", "AWB generated", "SLA"]}>
      {data.rows.map((r, i) => (
        <tr key={r.awb + i} className="hover:bg-surface-2/60">
          <td className={rn}>{offset + i + 1}</td>
          <td className={cx(td, "font-mono text-[13px] font-semibold")}>{r.awb}</td>
          <td className={cx(td, "font-mono text-xs")}>{r.order?.channel_order_id ?? "-"}</td>
          <td className={td}>{r.order ? <MarketChip name={r.order.channel_label} color={colorOf(r.order.channel_id ?? 0)} /> : "-"}</td>
          <td className={cx(td, "max-w-[220px] truncate text-xs")} title={r.order?.items.map((x) => `${x.sku} x${x.qty}`).join(", ")}>
            {r.order?.items.map((x) => x.sku).join(", ") ?? "-"}
          </td>
          <td className={cx(td, "tnum text-right")}>{r.order?.total_qty ?? "-"}</td>
          <td className={td}>{r.order?.courier || "-"}</td>
          <td className={cx(td, "whitespace-nowrap")}>
            {r.awb_generated_local || "-"}
            {r.age_days ? <span className="block text-xs font-semibold text-crit-ink">{r.age_days} day{r.age_days > 1 ? "s" : ""} old</span> : null}
          </td>
          <td className={cx(td, "whitespace-nowrap", r.sla_breached && "font-semibold text-crit-ink")}>
            {fmtDateTime(r.order?.sla_date)}
            {r.sla_breached && <span className="block text-xs">passed</span>}
          </td>
        </tr>
      ))}
    </Sheet>
  );
}

function SkuSheet({ rows }: { rows: SkuRow[] | null }) {
  if (!rows) return <SkeletonRows />;
  if (rows.length === 0) return <Empty title="No SKUs in this period" />;
  return (
    <Sheet minW={680} head={["SKU", "Units scanned", "Units pending", "Top marketplace", "Share"]}>
      {rows.map((r, i) => (
        <tr key={r.sku} className="hover:bg-surface-2/60">
          <td className={rn}>{i + 1}</td>
          <td className={cx(td, "font-mono text-[13px] font-semibold")}>{r.sku}</td>
          <td className={cx(td, "tnum text-right font-semibold")}>{r.scanned.toLocaleString("en-IN")}</td>
          <td className={cx(td, "tnum text-right", r.pending ? "font-semibold text-warn-ink" : "text-muted")}>{r.pending.toLocaleString("en-IN")}</td>
          <td className={cx(td, "max-w-[260px] truncate")}>{r.top_channel || "-"}</td>
          <td className={cx(td, "tnum text-right text-ink-2")}>{r.top_share === null ? "-" : `${r.top_share}%`}</td>
        </tr>
      ))}
    </Sheet>
  );
}

function OperatorSheet({ rows }: { rows: OperatorRow[] | null }) {
  if (!rows) return <SkeletonRows />;
  if (rows.length === 0) return <Empty title="No scans by anyone in this period" />;
  const time = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
  return (
    <Sheet minW={900} head={["Operator", "Scans", "Avg. scan time", "Success rate", "Flagged", "Stopped", "Stations", "First - last"]}>
      {rows.map((r, i) => (
        <tr key={r.user_id} className="hover:bg-surface-2/60">
          <td className={rn}>{i + 1}</td>
          <td className={td}>
            <span className="flex items-center gap-2.5">
              <span className="grid size-8 shrink-0 place-items-center rounded-full bg-accent-wash text-xs font-bold text-accent-ink" aria-hidden>
                {initials(r.name)}
              </span>
              <span className="font-semibold">{r.name}</span>
            </span>
          </td>
          <td className={cx(td, "tnum text-right font-semibold")}>{r.scans.toLocaleString("en-IN")}</td>
          <td className={cx(td, "tnum text-right")}>{r.avg_scan_seconds === null ? "-" : `${r.avg_scan_seconds < 10 ? r.avg_scan_seconds.toFixed(1) : Math.round(r.avg_scan_seconds)} sec`}</td>
          <td className={cx(td, "tnum text-right font-semibold text-good-ink")}>{r.success_rate === null ? "-" : `${r.success_rate}%`}</td>
          <td className={cx(td, "tnum text-right", r.flagged ? "text-warn-ink" : "text-muted")}>{r.flagged}</td>
          <td className={cx(td, "tnum text-right", r.rejected ? "text-crit-ink" : "text-muted")}>{r.rejected}</td>
          <td className={cx(td, "max-w-[160px] truncate text-xs")}>{r.stations.join(", ") || "-"}</td>
          <td className={cx(td, "whitespace-nowrap text-xs text-ink-2")}>
            {time(r.first)} - {time(r.last)}
          </td>
        </tr>
      ))}
    </Sheet>
  );
}

function AuditSheet({ rows }: { rows: EventRow[] | null }) {
  if (!rows) return <SkeletonRows />;
  if (rows.length === 0) return <Empty title="No scan activity on this day" />;
  return (
    <Sheet minW={980} head={["Time", "Operator", "Scanned", "Outcome", "Details", "Marketplace", "Station"]}>
      {rows.map((e, i) => (
        <tr key={e.id} className="align-top hover:bg-surface-2/60">
          <td className={rn}>{i + 1}</td>
          <td className={cx(td, "whitespace-nowrap font-mono text-xs")}>{e.local}</td>
          <td className={cx(td, "whitespace-nowrap font-semibold")}>{e.user}</td>
          <td className={cx(td, "font-mono text-[13px]")}>{e.tracking}</td>
          <td className={td}>
            <OutcomePill outcome={e.outcome} />
          </td>
          <td className={cx(td, "max-w-[340px] text-xs text-ink-2")}>{e.message}</td>
          <td className={cx(td, "max-w-[200px] truncate text-xs")}>{e.channel}</td>
          <td className={cx(td, "text-xs text-muted")}>{e.station || "-"}</td>
        </tr>
      ))}
    </Sheet>
  );
}
