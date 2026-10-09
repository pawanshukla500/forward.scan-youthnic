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
  /** unix seconds since OMSGuru stopped answering (their outage) */
  omsguru_down_since?: number | null;
  omsguru_state?: "ok" | "down" | "key_refused";
  waiting_for_credit: boolean;
  crosscheck_off?: boolean;
}

const STALE_MINUTES = 10;

export interface SyncState {
  tone: "crit" | "warn" | "info";
  icon: ReactNode;
  title: string;
  /** short label for the compact chip on the scan screen */
  short: string;
  body: ReactNode;
}

/** When the order data from OMSGuru cannot be trusted right now - and what that means for scans. Null = all fine. */
export function useSyncState(): SyncState | null {
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
    return {
      tone: "crit",
      icon: <CloudOff className="size-4" />,
      title: "Cannot reach the Forward Scan server",
      short: "Server offline",
      body: "scans are NOT being saved. Check this device's network or the server PC, then scan the packet again.",
    };
  if (!b || b.mode !== "live") return null;
  if (b.initial_load)
    return {
      tone: "info",
      icon: <CloudDownload className="size-4 text-accent-ink" />,
      title: "Loading orders from OMSGuru for the first time",
      short: "Loading orders",
      body: (
        <>
          {b.cached_orders.toLocaleString("en-IN")} orders so far. You can scan now: a packet whose order is not loaded yet shows{" "}
          <i>Not found - not saved</i>; keep it aside and scan it again in a few minutes.
        </>
      ),
    };
  if (b.omsguru_down_since) {
    const at = new Date(b.omsguru_down_since * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    return {
      tone: "crit",
      icon: <RefreshCwOff className="size-4" />,
      title:
        b.omsguru_state === "key_refused"
          ? `OMSGuru refuses our API key since ${at} - an admin must update the OMSGuru API token`
          : `OMSGuru is down since ${at} (their outage)`,
      short: b.omsguru_state === "key_refused" ? "OMSGuru key refused" : "OMSGuru down",
      body: (
        <>
          scanning works for every order synced before {at}. Labels made since then show <i>Not found - not saved</i>: keep those packets
          aside and scan them again when the sync is back. Nothing is missed: when it works again the app continues from {at} and re-checks
          the last 7 days by itself.
          {b.invoices_error && <span className="mt-1 block text-xs opacity-80">{b.invoices_error}</span>}
        </>
      ),
    };
  }
  if (b.invoices_failing)
    return {
      tone: "crit",
      icon: <RefreshCwOff className="size-4" />,
      title: "OMSGuru sync is failing",
      short: "Sync failing",
      body: (
        <>
          new AWBs are not coming in. Orders synced before still scan normally; labels made since then show{" "}
          <i>Not found - not saved</i> - keep those packets aside and scan them again once the sync is back. Tell a supervisor.
          {b.invoices_error && <span className="mt-1 block text-xs opacity-80">{b.invoices_error}</span>}
        </>
      ),
    };
  const staleMin = b.last_invoice_sync ? (Date.now() / 1000 - b.last_invoice_sync) / 60 : null;
  if (staleMin !== null && staleMin > STALE_MINUTES)
    return {
      tone: "warn",
      icon: <Hourglass className="size-4" />,
      title: `No new AWBs from OMSGuru for ${Math.floor(staleMin)} minutes`,
      short: `No new AWBs · ${Math.floor(staleMin)} min`,
      body: (
        <>
          {b.waiting_for_credit
            ? "the OMSGuru API is busy (60 calls per 5 minutes, shared with your other integrations). "
            : "the sync is behind. "}
          Labels made since then show <i>Not found - not saved</i>: keep those packets aside and scan them again in a few minutes.
        </>
      ),
    };
  if (b.waiting_for_credit)
    return {
      tone: "warn",
      icon: <Hourglass className="size-4" />,
      title: "OMSGuru API is busy",
      short: "OMSGuru busy",
      body: "(60 calls per 5 minutes, shared with your other integrations). Brand-new labels may show Not found for a few minutes - scan them again shortly.",
    };
  if (b.crosscheck_off)
    return {
      tone: "warn",
      icon: <Scale className="size-4" />,
      title: "Pending orders differ from OMSGuru",
      short: "Counts differ",
      body: "the local order list does not match OMSGuru's own count. Scanning works; a supervisor should check Admin > OMSGuru sync.",
    };
  return null;
}

/** Full banner. Phones: a one-line strip (icon + title + chevron); tap to expand the full text. */
export function SyncBanner({ state }: { state: SyncState }) {
  const [open, setOpen] = useState(false);
  const { tone, icon, title, body } = state;
  return (
    <div
      className={cx(
        "sync-notice flex items-start gap-2.5 rounded-lg px-3 py-2 text-sm md:gap-3 md:px-4 md:py-2",
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
        {open && <div className="pb-1 md:hidden">{body}</div>}
        <div className="hidden md:block">
          <b className={tone === "info" ? "text-ink" : undefined}>{title}</b> - {body}
        </div>
      </div>
    </div>
  );
}

/** Small status pill for the scan toolbar: the scan screen keeps its space for the packet, the full text is on hover / tap. */
export function SyncChip({ state }: { state: SyncState }) {
  const [open, setOpen] = useState(false);
  const plain = `${state.title} - ${typeof state.body === "string" ? state.body : ""}`.replace(/ - $/, "");
  return (
    <span className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={plain}
        className={cx(
          "inline-flex min-h-9 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full px-3 text-sm font-semibold",
          state.tone === "warn" ? "bg-warn-wash text-warn-ink" : "bg-info-wash text-ink-2",
        )}
      >
        <span aria-hidden>{state.icon}</span>
        {state.short}
      </button>
      {open && (
        <span
          role="status"
          className="flash-in absolute right-0 top-full z-30 mt-1 w-72 rounded-xl border border-line bg-surface p-3 text-sm text-ink-2 shadow-md"
        >
          <b className="text-ink">{state.title}</b> - {state.body}
        </span>
      )}
    </span>
  );
}

/** Banner for every state (marketplace picker page). */
export function SyncNotice() {
  const state = useSyncState();
  return state ? <SyncBanner state={state} /> : null;
}
