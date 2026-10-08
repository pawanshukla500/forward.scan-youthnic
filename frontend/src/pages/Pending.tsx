import { AlertOctagon, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, Download, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, download, fmtDateTime, qs, type AwbCounts, type Order, type Scan } from "../api";
import { AwbProgress } from "../components/AwbProgress";
import { Button, Card, ChannelDot, cx, Empty, inputCls, PageHeader, Spinner, Stat, SkeletonRows } from "../components/ui";
import { useLive, useThrottled } from "../live";

interface ChannelRec extends AwbCounts {
  id: number | null;
  name: string;
  marketplace: string;
  color: string;
}

interface Summary {
  date: string;
  is_today: boolean;
  today: string;
  retain_days: number;
  in_retention: boolean;
  totals: AwbCounts;
  channels: ChannelRec[];
  days: (Omit<AwbCounts, "pct"> & { date: string })[];
  oms_check: { ok: boolean; retry: boolean; checked_at: number; oms: number; local: number } | null;
  counted_from: string | null;
}

interface Row {
  awb: string;
  bucket: string;
  status: string;
  awb_generated_at: string | null;
  awb_generated_local: string;
  age_days: number | null;
  sla_breached: boolean;
  /** pending here, although OMS already shows it shipped / in transit */
  shipped_in_oms?: boolean;
  order: Order | null;
  scan: Scan | null;
}

const BUCKETS = [
  { key: "pending", label: "Pending", hint: "AWB generated on this day and not scanned here yet - also when OMS already shows it shipped" },
  { key: "overdue", label: "Overdue", hint: "AWB generated on an EARLIER day and still not dispatched" },
  { key: "left_unscanned", label: "Shipped in OMS, not scanned", hint: "Part of Pending: OMS already shows it shipped / in transit, but it was never scanned here - stays pending until it is" },
  { key: "cancelled", label: "Cancelled after AWB", hint: "Cancelled or returned after the label was made - do not ship" },
  { key: "scanned", label: "Scanned", hint: "Forward-scanned" },
  { key: "generated", label: "All AWBs", hint: "Every AWB generated on this day" },
] as const;

const OMS_STATUS: Record<string, string> = {
  OPEN: "Ready to ship", NOT_PACKED: "Not packed", PARTIAL_CANCEL: "Partly cancelled", MOVED: "Left Ready-to-ship",
  SHIPPED: "Shipped / in transit", CANCELLED: "Cancelled", RETURN: "Return", UNKNOWN: "Unknown",
};

function dayLabel(iso: string, today: string) {
  const d = new Date(iso + "T00:00:00");
  if (iso === today) return "Today";
  const y = new Date(today + "T00:00:00");
  y.setDate(y.getDate() - 1);
  if (d.getTime() === y.getTime()) return "Yesterday";
  return d.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" });
}

function OmsCheck({ c }: { c: NonNullable<Summary["oms_check"]> }) {
  const at = new Date(c.checked_at * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
  return (
    <span className={cx("mt-1 flex items-center gap-1.5 text-sm font-medium", c.ok ? "text-good-ink" : "text-warn-ink")}>
      {c.ok ? <CheckCircle2 className="size-4" aria-hidden /> : <CircleAlert className="size-4" aria-hidden />}
      {c.ok
        ? `Packed + Ready-to-ship orders match OMSGuru (${c.oms.toLocaleString("en-IN")}) - checked ${at}`
        : `Differs from OMSGuru at ${at}: ${c.local.toLocaleString("en-IN")} here vs ${c.oms.toLocaleString("en-IN")} - ${c.retry ? "refreshing again" : "see Admin > OMSGuru sync"}`}
    </span>
  );
}

export default function Pending() {
  const [sp, setSp] = useSearchParams();
  const date = sp.get("date") || "";
  const bucket = sp.get("bucket") || "pending";
  const channelId = sp.get("channel_id") || "";
  const [page, setPage] = useState(1);
  const [sum, setSum] = useState<Summary | null>(null);
  const [list, setList] = useState<{ total: number; rows: Row[] } | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (patch: Record<string, string>) => {
    const next = new URLSearchParams(sp);
    Object.entries(patch).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    setSp(next, { replace: true });
    setPage(1);
  };

  const load = useCallback(() => {
    setBusy(true);
    Promise.all([
      api<Summary>(`/api/reconciliation${qs({ date })}`).then(setSum),
      api<{ total: number; rows: Row[] }>(`/api/reconciliation/list${qs({ date, bucket, channel_id: channelId, page, page_size: 100 })}`).then(setList),
    ]).finally(() => setBusy(false));
  }, [date, bucket, channelId, page]);

  useEffect(load, [load]);
  const refreshSoon = useThrottled(load, 1500);
  useLive((event) => {
    if (!["scan", "scan_voided", "scan_updated", "sync"].includes(event)) return;
    refreshSoon();
  });

  if (!sum) return <Spinner />;
  const t = sum.totals;
  const pages = list ? Math.max(1, Math.ceil(list.total / 100)) : 1;
  const counts: Record<string, number> = {
    pending: t.pending, overdue: t.overdue, left_unscanned: t.left_unscanned, cancelled: t.cancelled, scanned: t.scanned, generated: t.generated,
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Pending & reconciliation"
        sub={
          <>
            Every AWB generated in OMSGuru vs what was forward-scanned, by sales channel. An AWB generated today must be dispatched today.
            {sum.counted_from && (
              <span className="mt-1 block text-sm text-muted">
                Counting orders packed from{" "}
                <b className="text-ink">
                  {new Date(sum.counted_from + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}
                </b>{" "}
                (set in Admin)
              </span>
            )}
            {sum.oms_check && <OmsCheck c={sum.oms_check} />}
          </>
        }
        actions={
          <>
            <Button onClick={load} title="Refresh">
              <RefreshCw className={cx("size-4", busy && "animate-spin")} />
            </Button>
            <Button
              variant="primary"
              onClick={() => void download(`/api/reconciliation/export.xlsx${qs({ date, bucket, channel_id: channelId })}`).catch((e) => alert(e.message))}
            >
              <Download className="size-4" /> Export
            </Button>
          </>
        }
      />

      {/* day strip */}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {[...sum.days].reverse().map((d) => {
          const active = d.date === sum.date;
          const base = d.generated - d.cancelled;
          const pct = base > 0 ? Math.round((100 * d.scanned) / base) : null;
          return (
            <button
              key={d.date}
              onClick={() => set({ date: d.date === sum.today ? "" : d.date })}
              className={cx(
                "min-w-[132px] shrink-0 rounded-xl border px-3 py-2 text-left transition",
                active ? "border-accent bg-info-wash" : "border-line bg-surface hover:bg-surface-2",
              )}
            >
              <div className="text-xs font-medium text-ink-2">{dayLabel(d.date, sum.today)}</div>
              <div className="tnum text-lg font-semibold">{d.generated.toLocaleString("en-IN")}</div>
              <div className="text-xs text-muted">
                AWBs · {pct === null ? "-" : `${pct}% scanned`}
                {d.pending > 0 && <span className="text-warn-ink"> · {d.pending} pending</span>}
              </div>
            </button>
          );
        })}
      </div>
      {!sum.in_retention && (
        <div className="rounded-lg bg-info-wash px-4 py-2.5 text-sm text-ink-2">
          Order data is kept for {sum.retain_days} days, so AWB counts are not available for this date. Scanned shipments are in Scans & reports.
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label={`AWBs generated ${sum.is_today ? "today" : "on " + sum.date}`} value={t.generated.toLocaleString("en-IN")} />
        <Stat label="Scanned" value={t.scanned.toLocaleString("en-IN")} tone="good" hint={t.pct !== null ? `${t.pct}% of dispatchable` : undefined} />
        <Stat label={sum.is_today ? "Pending - due today" : "Still pending"} value={t.pending.toLocaleString("en-IN")} tone={t.pending ? "warn" : undefined} />
        <Stat label="Overdue (earlier days)" value={t.overdue.toLocaleString("en-IN")} tone={t.overdue ? "crit" : undefined} hint="AWB older, still not dispatched" />
        <Stat label="Of pending: shipped in OMS" value={t.left_unscanned.toLocaleString("en-IN")} tone={t.left_unscanned ? "warn" : undefined} hint="OMS says shipped, never scanned here" />
        <Stat label="Cancelled after AWB" value={t.cancelled.toLocaleString("en-IN")} hint="excluded from pending" />
      </div>

      <Card>
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">By sales channel</h2>
          <span className="text-xs text-muted">click a number to list those AWBs</span>
        </div>
        {sum.channels.length === 0 ? (
          <Empty>No AWBs generated on this day yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="px-4 py-2 font-medium">Sales channel</th>
                  <th className="px-3 py-2 text-right font-medium">Generated</th>
                  <th className="px-3 py-2 text-right font-medium">Scanned</th>
                  <th className="px-3 py-2 text-right font-medium">Pending</th>
                  <th className="px-3 py-2 text-right font-medium">Overdue</th>
                  <th className="px-3 py-2 text-right font-medium">Of pending: shipped in OMS</th>
                  <th className="px-3 py-2 text-right font-medium">Cancelled</th>
                  <th className="w-56 px-4 py-2 font-medium">Progress</th>
                </tr>
              </thead>
              <tbody className="tnum divide-y divide-line">
                {sum.channels.map((c) => {
                  const cell = (b: string, v: number, cls = "") =>
                    v ? (
                      <button className={cx("font-medium hover:underline", cls)} onClick={() => set({ bucket: b, channel_id: c.id ? String(c.id) : "" })}>
                        {v.toLocaleString("en-IN")}
                      </button>
                    ) : (
                      <span className="text-muted">0</span>
                    );
                  return (
                    <tr key={String(c.id)} className={cx("hover:bg-surface-2/60", channelId === String(c.id) && "bg-info-wash/60")}>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          <ChannelDot color={c.color} />
                          <div className="min-w-0">
                            <div className="truncate font-medium">{c.name}</div>
                            <div className="truncate text-xs text-muted">{c.marketplace}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 text-right">{cell("generated", c.generated)}</td>
                      <td className="px-3 text-right">{cell("scanned", c.scanned, "text-good-ink")}</td>
                      <td className="px-3 text-right">{cell("pending", c.pending, "text-warn-ink")}</td>
                      <td className="px-3 text-right">{cell("overdue", c.overdue, "text-crit-ink")}</td>
                      <td className="px-3 text-right">{cell("left_unscanned", c.left_unscanned)}</td>
                      <td className="px-3 text-right">{cell("cancelled", c.cancelled)}</td>
                      <td className="px-4 py-2">
                        <AwbProgress c={c} bare />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="flex flex-wrap gap-1">
            {BUCKETS.map((b) => (
              <button
                key={b.key}
                title={b.hint}
                onClick={() => set({ bucket: b.key })}
                className={cx(
                  "ease-ui min-h-10 cursor-pointer rounded-full px-3.5 text-sm font-medium",
                  bucket === b.key ? "bg-ink text-surface" : "text-ink-2 hover:bg-surface-2",
                )}
              >
                {b.label} <span className="tnum opacity-75">{(counts[b.key] ?? 0).toLocaleString("en-IN")}</span>
              </button>
            ))}
          </div>
          <select className={`${inputCls} h-9`} value={channelId} onChange={(e) => set({ channel_id: e.target.value })} aria-label="Sales channel">
            <option value="">All channels</option>
            {sum.channels.filter((c) => c.id).map((c) => (
              <option key={String(c.id)} value={String(c.id)}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <p className="px-4 pt-3 text-xs text-muted">{BUCKETS.find((b) => b.key === bucket)?.hint}</p>
        {!list ? (
          <SkeletonRows />
        ) : list.rows.length === 0 ? (
          <Empty>Nothing here.</Empty>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1050px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="px-4 py-2 font-medium">AWB</th>
                    <th className="px-3 py-2 font-medium">Sales channel</th>
                    <th className="px-3 py-2 font-medium">Order</th>
                    <th className="px-3 py-2 font-medium">Courier</th>
                    <th className="px-3 py-2 font-medium">SKUs</th>
                    <th className="px-3 py-2 font-medium">AWB generated</th>
                    <th className="px-3 py-2 font-medium">SLA</th>
                    <th className="px-3 py-2 font-medium">OMS status</th>
                    <th className="px-3 py-2 font-medium">Scan</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {list.rows.map((r) => (
                    <tr key={r.awb} className={cx("align-top", (r.bucket === "overdue" || r.sla_breached) && !r.scan && "bg-crit-wash/40")}>
                      <td className="px-4 py-2.5 font-mono">{r.awb}</td>
                      <td className="max-w-[200px] truncate px-3 py-2.5">{r.order?.channel_label}</td>
                      <td className="px-3 py-2.5 font-mono text-xs">{r.order?.channel_order_id}</td>
                      <td className="px-3 py-2.5">{r.order?.courier || "-"}</td>
                      <td className="px-3 py-2.5 text-xs">{r.order?.items.map((i) => `${i.sku} x${i.qty}`).join(", ")}</td>
                      <td className="whitespace-nowrap px-3 py-2.5">
                        {r.awb_generated_local}
                        {r.age_days ? <div className="text-xs font-semibold text-crit-ink">{r.age_days} day{r.age_days > 1 ? "s" : ""} old</div> : null}
                      </td>
                      <td className={cx("whitespace-nowrap px-3 py-2.5", r.sla_breached && !r.scan ? "font-semibold text-crit-ink" : "text-ink-2")}>
                        {fmtDateTime(r.order?.sla_date)}
                        {r.sla_breached && !r.scan && (
                          <div className="inline-flex items-center gap-1 text-xs">
                            <AlertOctagon className="size-3" aria-hidden /> SLA breached
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-ink-2">
                        {OMS_STATUS[r.status] ?? r.status}
                        {r.shipped_in_oms && (
                          <span className="ml-1.5 inline-block rounded-full bg-warn-wash px-2 py-0.5 text-xs font-bold text-warn-ink" title="OMS already shows it shipped, but it was never scanned here">
                            not scanned
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-ink-2">{r.scan ? `${r.scan.scanned_at_local} · ${r.scan.user}` : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-line px-4 py-3 text-sm text-ink-2">
              <span>{list.total.toLocaleString("en-IN")} AWBs</span>
              <div className="flex items-center gap-2">
                <Button size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} aria-label="Previous page"><ChevronLeft className="size-4" /></Button>
                <span className="tnum">{page} / {pages}</span>
                <Button size="sm" disabled={page >= pages} onClick={() => setPage((p) => p + 1)} aria-label="Next page"><ChevronRight className="size-4" /></Button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
