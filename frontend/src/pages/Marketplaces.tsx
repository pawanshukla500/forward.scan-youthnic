import { ChevronRight, Settings2, Store } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AwbCounts, type Channel } from "../api";
import { isSupervisor, useAuth } from "../App";
import { Empty, Skeleton } from "../components/ui";
import { useLive, useThrottled } from "../live";

interface MarketChannel extends Omit<Channel, "today"> {
  awb_7d: number;
  last_synced_at: string | null;
  scans_today: number;
  today: Omit<AwbCounts, "pct">;
  pct: number | null;
}

interface MarketData {
  channels: MarketChannel[];
  sync_ok: boolean;
  retain_days: number;
}

export function fmtAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "Never";
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export default function Marketplaces() {
  const { user } = useAuth();
  const [data, setData] = useState<MarketData | null>(null);
  const [now, setNow] = useState(Date.now());

  const load = useCallback(() => {
    api<MarketData>("/api/marketplaces").then((d) => {
      setData(d);
      setNow(Date.now());
    });
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(t);
  }, []);
  const refreshSoon = useThrottled(load, 1500);
  useLive((event) => {
    if (!["scan", "scan_voided", "sync"].includes(event)) return;
    refreshSoon();
  });

  const active = data?.channels.filter((c) => c.scan_enabled) ?? [];
  const off = data?.channels.filter((c) => !c.scan_enabled) ?? [];

  return (
    <div>
      <div className="page-toolbar">
        <div>
          <b>Connected channels</b>
          <span>
            {data ? `${active.length} scanning · ${data.channels.length} synced from OMSGuru · order data kept ${data.retain_days} days` : "Loading..."}
          </span>
        </div>
        {isSupervisor(user) && (
          <Link
            to="/admin"
            className="primary"
          >
            <Settings2 className="size-4" aria-hidden /> Manage channels
          </Link>
        )}
      </div>

      {!data ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-60 w-full" />
          ))}
        </div>
      ) : data.channels.length === 0 ? (
        <div className="card">
          <Empty title="No sales channels yet" icon={Store}>
            Channels appear here after the first OMSGuru sync.
          </Empty>
        </div>
      ) : (
        <>
          <div className="market-grid">
            {active.map((c) => (
              <MarketCard key={c.id} c={c} syncOk={data.sync_ok} now={now} />
            ))}
          </div>
          {off.length > 0 && (
            <section className="card p-5">
              <span className="eyebrow">Not scanned here</span>
              <h2 className="mt-1 text-[17px] font-bold">Scanning turned off ({off.length})</h2>
              <p className="text-sm text-muted">These OMSGuru sales channels are synced but hidden from the scan screen.</p>
              <ul className="mt-3 flex flex-wrap gap-2">
                {off.map((c) => (
                  <li key={c.id} className="inline-flex items-center gap-2 rounded-full bg-surface-2 px-3 py-1 text-sm text-ink-2">
                    <span className="size-2 rounded-full" style={{ background: c.color }} aria-hidden />
                    {c.name}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function MarketCard({ c, syncOk, now }: { c: MarketChannel; syncOk: boolean; now: number }) {
  const base = c.today.generated - c.today.cancelled;
  const stale = !c.last_synced_at || now - new Date(c.last_synced_at).getTime() > 6 * 3600 * 1000;
  return (
    <section className="card market-card" aria-label={c.name}>
      <div className="market-logo" style={{ background: c.color || "#78a940" }}>
        {(c.marketplace || c.name).slice(0, 1).toUpperCase()}
      </div>
      <div className="market-top">
        <div>
          <h2 title={c.name}>{c.name}</h2>
          <p>{[c.marketplace, c.company].filter(Boolean).join(" · ")}</p>
        </div>
        <span className="status success">
          <i />
          {c.oms_status || "Connected"}
        </span>
      </div>

      <div className="market-stats">
        <div>
          <span>Orders synced</span>
          <b>{c.awb_7d.toLocaleString("en-IN")}</b>
        </div>
        <div>
          <span>Last sync</span>
          <b>{fmtAgo(c.last_synced_at, now)}</b>
        </div>
      </div>
      <div className="market-footer">
        <span>
          <i />
          {syncOk ? "Live sync active" : "Sync delayed"}
          {c.today.pending ? ` · ${c.today.pending} pending` : ""}
          {base ? ` · ${c.today.scanned}/${base} today` : ""}
          {stale ? " · stale" : ""}
        </span>
        <Link to={`/scan/${c.id}`}>
          Scan <ChevronRight className="size-3.5" aria-hidden />
        </Link>
      </div>
    </section>
  );
}
