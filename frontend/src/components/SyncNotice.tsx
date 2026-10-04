import { ChevronDown, CloudDownload, CloudOff, Hourglass, RefreshCwOff, Scale } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { useLive } from "../live";
import { cx } from "./ui";

interface Brief {
  mode: string;
  initial_load: boolean;
  cached_orders: number;
  last_invoice_sync: number | null;
  sync_interval_seconds?: number;
  invoices_failing?: boolean;
  invoices_error?: string;
  waiting_for_credit: boolean;
  crosscheck_off?: boolean;
}

const STALE_MINUTES = 10;

function Notice({ tone, icon, title, children }: { tone: "crit" | "warn" | "info"; icon: ReactNode; title: string; children: ReactNode }) {
  // Phones: a one-line strip (icon + title + chevron); tap to expand the full text.
  // The full banner ate half the scan screen on mobile.
  const [open, setOpen] = useState(false);
  return (
    <div
      className={cx(
        "sync-notice flex items-start gap-2.5 rounded-lg px-3 py-2 text-sm md:gap-3 md:px-4 md:py-3",
        tone === "crit" ? "bg-crit-wash text-crit-ink" : tone === "warn" ? "bg-warn-wash text-warn-ink" : "bg-info-wash text-ink-2",
      )}
      role={tone === "crit" ? "alert" : "status"}
    >
      <span className="mt-0.5 shrink-0" aria-hidden>
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex min-h-9 w-full cursor-pointer items-center gap-1.5 text-left md:hidden"
        >
          <b className="min-w-0 flex-1 truncate">{title}</b>
          <ChevronDown className={cx("size-4 shrink-0 transition-transform", open && "rotate-180")} aria-hidden />
        </button>
        {open && <div className="pb-1 md:hidden">{children}</div>}
        <div className="hidden md:block">
          <b className={tone === "info" ? "text-ink" : undefined}>{title}</b> - {children}
        </div>
      </div>
    </div>
  );
}

/** Tells scan stations when the order data from OMSGuru cannot be trusted right now - and what that means for scans. */
export function SyncNotice() {
  const [b, setB] = useState<Brief | null>(null);
  const [offline, setOffline] = useState(false);
  const load = useCallback(() => {
    api<Brief>("/api/sync/brief")
      .then((r) => {
        setB(r);
        setOffline(false);
      })
      .catch((e: Error) => setOffline(e instanceof TypeError || /fetch|network/i.test(e.message)));
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 8000);
    return () => window.clearInterval(t);
  }, [load]);
  useLive((event) => {
    if (event === "sync") load();
  });

  if (offline)
    return (
      <Notice tone="crit" icon={<CloudOff className="size-4" />} title="Cannot reach the Forward Scan server">
        scans are NOT being saved. Check this device's network or the server PC, then scan the packet again.
      </Notice>
    );
  if (!b || b.mode !== "live") return null;
  if (b.initial_load)
    return (
      <Notice tone="info" icon={<CloudDownload className="size-4 text-accent-ink" />} title="Loading orders from OMSGuru for the first time">
        {b.cached_orders.toLocaleString("en-IN")} orders so far. You can scan now: anything not loaded yet is saved as <i>Not found</i> and turns green by
        itself when its order arrives.
      </Notice>
    );
  if (b.invoices_failing)
    return (
      <Notice tone="crit" icon={<RefreshCwOff className="size-4" />} title="OMSGuru sync is failing">
        new AWBs are not coming in, so new labels show as <i>Not found</i> (scans are still saved and verify once it recovers). Tell a supervisor.
        {b.invoices_error && <span className="mt-1 block text-xs opacity-80">{b.invoices_error}</span>}
      </Notice>
    );
  const staleMin = b.last_invoice_sync ? (Date.now() / 1000 - b.last_invoice_sync) / 60 : null;
  if (staleMin !== null && staleMin > STALE_MINUTES)
    return (
      <Notice tone="warn" icon={<Hourglass className="size-4" />} title={`No new AWBs from OMSGuru for ${Math.floor(staleMin)} minutes`}>
        {b.waiting_for_credit
          ? "the OMSGuru API is busy (60 calls per 5 minutes, shared with your other integrations). "
          : "the sync is behind. "}
        Labels made since then show as <i>Not found</i>; scans are still saved and verify by themselves.
      </Notice>
    );
  if (b.waiting_for_credit)
    return (
      <Notice tone="warn" icon={<Hourglass className="size-4" />} title="OMSGuru API is busy">
        (60 calls per 5 minutes, shared with your other integrations). New orders may take a few minutes to verify; scans are still saved.
      </Notice>
    );
  if (b.crosscheck_off)
    return (
      <Notice tone="warn" icon={<Scale className="size-4" />} title="Pending orders differ from OMSGuru">
        the local order list does not match OMSGuru's own count. Scanning works; a supervisor should check Admin &gt; OMSGuru sync.
      </Notice>
    );
  return null;
}
