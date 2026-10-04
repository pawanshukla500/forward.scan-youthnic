import { ArrowRight, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Channel } from "../api";
import { AwbProgress } from "../components/AwbProgress";
import { SyncNotice } from "../components/SyncNotice";
import { ChannelDot, Empty, inputCls, PageHeader, Spinner } from "../components/ui";
import { useLive, useThrottled } from "../live";

export default function ChannelPicker() {
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [date, setDate] = useState("");
  const [q, setQ] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api<{ date: string; channels: Channel[] }>("/api/channels")
      .then((r) => {
        setChannels(r.channels);
        setDate(r.date);
      })
      .catch((e) => setErr(e.message));
  }, []);

  useEffect(load, [load]);
  const refreshSoon = useThrottled(load, 1500);
  useLive((event) => {
    if (!["scan", "scan_voided", "scan_updated", "sync"].includes(event)) return;
    refreshSoon();
  });

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    return (channels ?? []).filter((c) => !s || `${c.name} ${c.marketplace} ${c.company}`.toLowerCase().includes(s));
  }, [channels, q]);

  return (
    <div>
      <PageHeader
        title="Choose a sales channel"
        sub={<>Scan one marketplace's bag at a time. A shipment that belongs to another channel will be rejected. {date && <span className="text-muted">Dispatch date {date}</span>}</>}
      />
      <div className="mb-4 empty:hidden">
        <SyncNotice />
      </div>
      <div className="relative mb-4 max-w-md">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
        <input className={`${inputCls} w-full pl-9`} placeholder="Filter channels" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {err && <p className="mb-4 rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink">{err}</p>}
      {!channels ? (
        <Spinner />
      ) : shown.length === 0 ? (
        <div className="card">
          <Empty>No channels enabled for scanning yet. An admin can enable them in Admin &rarr; Sales channels (they load automatically from OMSGuru).</Empty>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {shown.map((c) => (
            <Link
              key={c.id}
              to={`/scan/${c.id}`}
              className="card group flex items-center gap-4 p-4 transition hover:border-accent hover:shadow-sm"
            >
              <span className="h-12 w-1.5 shrink-0 rounded-full" style={{ background: c.color || "#898781" }} aria-hidden />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <ChannelDot color={c.color} size={8} />
                  <span className="truncate text-xs font-medium uppercase tracking-wide text-ink-2">{c.marketplace}</span>
                </div>
                <div className="truncate text-base font-semibold">{c.name}</div>
                <div className="mt-1 flex flex-wrap gap-x-4 text-sm text-ink-2">
                  <span>
                    <b className="text-ink">{(c.today ?? 0).toLocaleString("en-IN")}</b> scanned today
                  </span>
                  {c.awb_today && (
                    <>
                      <span>
                        <b className="text-warn-ink">{c.awb_today.pending.toLocaleString("en-IN")}</b> pending
                      </span>
                      {c.awb_today.overdue > 0 && (
                        <span>
                          <b className="text-crit-ink">{c.awb_today.overdue.toLocaleString("en-IN")}</b> overdue
                        </span>
                      )}
                    </>
                  )}
                </div>
                {c.awb_today && c.awb_today.generated > 0 && (
                  <div className="mt-2">
                    <AwbProgress c={{ ...c.awb_today, overdue: 0 }} compact title="AWBs today" />
                  </div>
                )}
              </div>
              <ArrowRight className="size-5 text-muted transition group-hover:translate-x-0.5 group-hover:text-accent-ink" aria-hidden />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
