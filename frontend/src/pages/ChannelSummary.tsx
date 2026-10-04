import { Download } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, download, qs } from "../api";
import { ColumnChart } from "../components/ColumnChart";
import { Button, Card, ChannelDot, cx, Empty, Field, inputCls, Spinner } from "../components/ui";

interface Summary {
  date_from: string;
  date_to: string;
  group: "day" | "month";
  channels: { id: number; name: string; marketplace: string; color: string }[];
  rows: { period: string; total: number; by_channel: Record<string, Record<string, number>> }[];
  totals: Record<string, number>;
  grand_total: number;
}

function iso(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function periodLabel(p: string, group: "day" | "month") {
  if (group === "month") return new Date(p + "-01T00:00:00").toLocaleDateString("en-IN", { month: "short", year: "numeric" });
  return new Date(p + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
}

/** Long-term, channel-wise scanned shipments (kept SCAN_RETENTION_DAYS, default 3 years). */
export default function ChannelSummary() {
  const [group, setGroup] = useState<"day" | "month">("day");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [data, setData] = useState<Summary | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setData(null);
    const today = new Date();
    const f = from || iso(group === "month" ? new Date(today.getFullYear() - 1, today.getMonth() + 1, 1) : new Date(today.getTime() - 29 * 86400000));
    api<Summary>(`/api/reports/channel-summary${qs({ date_from: f, date_to: to, group })}`)
      .then((d) => {
        setData(d);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [group, from, to]);

  const chart = useMemo(
    () => (data?.rows ?? []).map((r) => ({ key: r.period, label: periodLabel(r.period, data!.group), tip: periodLabel(r.period, data!.group), value: r.total })),
    [data],
  );

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Field label="Group by">
            <select className={inputCls} value={group} onChange={(e) => setGroup(e.target.value as "day" | "month")}>
              <option value="day">Day</option>
              <option value="month">Month</option>
            </select>
          </Field>
          <Field label="From (scan date)">
            <input type="date" className={inputCls} value={from} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field label="To">
            <input type="date" className={inputCls} value={to} onChange={(e) => setTo(e.target.value)} />
          </Field>
          <div className="flex items-end">
            <Button
              className="w-full"
              onClick={() => void download(`/api/reports/channel-summary.xlsx${qs({ date_from: data?.date_from, date_to: data?.date_to, group })}`).catch((e) => alert(e.message))}
              disabled={!data}
            >
              <Download className="size-4" /> Export Excel
            </Button>
          </div>
        </div>
        <p className="mt-2 text-xs text-muted">Scanned shipments are kept per sales channel for the long term (by scan date). Leave dates empty for the last 30 days / 12 months.</p>
      </Card>

      {err && <p className="rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink">{err}</p>}
      {!data ? (
        !err && <Spinner />
      ) : data.rows.length === 0 || data.grand_total === 0 ? (
        <Card>
          <Empty>No scans in this period.</Empty>
        </Card>
      ) : (
        <>
          <Card className="p-4">
            <h2 className="text-sm font-semibold">
              Shipments scanned per {data.group} - {data.grand_total.toLocaleString("en-IN")} in total
            </h2>
            <p className="mb-3 text-xs text-muted">
              {data.date_from} to {data.date_to}, all sales channels
            </p>
            <ColumnChart data={chart} labelEvery={Math.max(1, Math.ceil(chart.length / 12))} caption={`Shipments scanned per ${data.group}`} />
          </Card>
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="sticky left-0 bg-surface px-4 py-2 font-medium">{data.group === "month" ? "Month" : "Day"}</th>
                    {data.channels.map((c) => (
                      <th key={c.id} className="min-w-[120px] px-3 py-2 text-right font-medium">
                        <span className="inline-flex items-center justify-end gap-1.5">
                          <ChannelDot color={c.color} size={8} />
                          <span className="max-w-[160px] truncate" title={c.name}>
                            {c.name}
                          </span>
                        </span>
                      </th>
                    ))}
                    <th className="px-4 py-2 text-right font-medium">Total</th>
                  </tr>
                </thead>
                <tbody className="tnum divide-y divide-line">
                  {[...data.rows].reverse().map((r) => (
                    <tr key={r.period} className={cx("hover:bg-surface-2/60", r.total === 0 && "text-muted")}>
                      <td className="sticky left-0 whitespace-nowrap bg-surface px-4 py-2">{periodLabel(r.period, data.group)}</td>
                      {data.channels.map((c) => (
                        <td key={c.id} className="px-3 py-2 text-right">
                          {(r.by_channel[String(c.id)]?.scanned ?? 0).toLocaleString("en-IN")}
                        </td>
                      ))}
                      <td className="px-4 py-2 text-right font-semibold">{r.total.toLocaleString("en-IN")}</td>
                    </tr>
                  ))}
                  <tr className="border-t-2 border-line-strong font-semibold">
                    <td className="sticky left-0 bg-surface px-4 py-2">Total</td>
                    {data.channels.map((c) => (
                      <td key={c.id} className="px-3 py-2 text-right">
                        {(data.totals[String(c.id)] ?? 0).toLocaleString("en-IN")}
                      </td>
                    ))}
                    <td className="px-4 py-2 text-right">{data.grand_total.toLocaleString("en-IN")}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
