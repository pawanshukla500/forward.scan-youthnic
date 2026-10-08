import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Copy,
  Flag,
  OctagonX,
  SearchX,
  Truck,
  Undo2,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { FLAG_LABELS, fmtDateTime, fmtMoney, FLAG_REASONS, type Order, type ScanContext, type ScanResponse } from "../api";
import type { Cue } from "../sound";
import { Button, cx, FlagChips } from "./ui";

export const CODE_TITLE: Record<string, string> = {
  OK: "Verified - OK to dispatch",
  DUPLICATE: "Duplicate scan",
  WRONG_CHANNEL: "Wrong marketplace",
  CANCELLED: "Cancelled - do not ship",
  RETURN: "Return - do not ship",
  INVALID: "Invalid barcode",
  WRONG_BARCODE: "Wrong barcode - scan the AWB",
  NOT_IN_OMS: "Not found - flagged, not counted",
  NOT_RTS: "Accepted - not packed in OMS",
  PARTIAL_CANCEL: "Accepted - partly cancelled",
  ALREADY_SHIPPED_IN_OMS: "Accepted - already shipped in OMS",
  STATUS_CHANGED: "Accepted - OMS status changed",
  CHANNEL_UNMAPPED: "Accepted - channel not mapped",
  NETWORK: "Not saved - connection error",
  LINKED: "Linked - earlier scan verified",
  AMBIGUOUS: "Scan the AWB barcode",
  ALERT: "Stop - order changed in OMS",
  FLAGGED: "Flagged for review",
  ALREADY_SAVED: "Already saved - your own scan a moment ago",
  REPLACED: "Old label - OMSGuru replaced this AWB",
};

/** What a scan result means on the floor: one colour and one sound per kind (see sound.ts). */
export type ScanKind = Cue;

export function scanKind(res: Pick<ScanResponse, "severity" | "code">): ScanKind {
  if (res.code === "DUPLICATE") return "duplicate";
  if (res.code === "NOT_IN_OMS") return "notfound";
  if (res.severity === "success") return "ok";
  if (res.severity === "warning") return "check";
  return "stop";
}

export const KIND_META: Record<ScanKind, { title: string; action: string; sound: string; icon: typeof Check }> = {
  ok: { title: "OK", action: "Verified - put it in the bag", sound: "1 beep", icon: CheckCircle2 },
  duplicate: { title: "Duplicate", action: "Already scanned - not counted again. Set this packet aside.", sound: "3 quick beeps", icon: Copy },
  notfound: { title: "Not found", action: "Flagged - NOT counted as scanned. OMSGuru does not have this AWB yet: keep the packet aside for a supervisor (it counts by itself if the order syncs)", sound: "beep-boop (high-low)", icon: SearchX },
  check: { title: "Check", action: "Saved - check the packet before it goes", sound: "2 beeps", icon: AlertTriangle },
  stop: { title: "Stop", action: "Not saved - put this packet aside", sound: "buzzer", icon: OctagonX },
};

/** A "Check" (amber) scan is saved, but something about the order needs a person: what to do, per reason
    (the verdict band above already says what is wrong). */
export const CHECK_HELP: Record<string, { todo: string }> = {
  NOT_RTS: { todo: "Keep the packet and tell a supervisor - the order must be marked Ready to ship in OMSGuru before the courier pickup." },
  PARTIAL_CANCEL: { todo: "Open the packet and take out the items marked Cancelled in the list below before it goes." },
  STATUS_CHANGED: { todo: "Check the OMS status below with a supervisor before the packet goes." },
  CHANNEL_UNMAPPED: { todo: "The scan is saved and counted. An admin should link this OMSGuru channel in Marketplaces." },
  FLAGGED: { todo: "Put the packet aside for a supervisor." },
};

/** The reasons behind a Check verdict that have advice, in the order the server gave them. */
export function checkReasons(res: Pick<ScanResponse, "code" | "scan">): string[] {
  const codes = [...(res.scan?.flags ?? []), res.code];
  return [...new Set(codes)].filter((c) => CHECK_HELP[c]);
}

/** "1h 24m" / "42 min" */
export function fmtMins(m: number): string {
  const a = Math.abs(Math.round(m));
  if (a < 60) return `${a} min`;
  const h = Math.floor(a / 60);
  if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return `${h}h ${String(a % 60).padStart(2, "0")}m`;
}

export function skuInitials(sku: string): string {
  const parts = sku.split(/[-_\s/.]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase();
}

export function orderShape(order: Order | null | undefined) {
  if (!order) return { skus: 0, units: 0, multi: false };
  const skus = new Set(order.items.map((i) => i.sku)).size || order.item_count;
  const units = order.total_qty || order.items.reduce((a, i) => a + (i.qty || 0), 0);
  return { skus, units, multi: skus > 1 || units > 1 };
}

function Detail({ k, children, sub }: { k: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs leading-4 font-medium text-muted">{k}</dt>
      <dd className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[15px] leading-5 font-semibold">{children || "-"}</dd>
      {sub && <dd className="truncate text-xs leading-4 text-muted">{sub}</dd>}
    </div>
  );
}

function FlagMenu({ onPick, busy }: { onPick: (reason: string) => void; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !box.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", close);
    };
  }, [open]);
  return (
    <div className="relative" ref={box}>
      <Button size="sm" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="menu" loading={busy}>
        <Flag className="size-4" aria-hidden /> Flag issue
      </Button>
      {/* the button sits in the bar under the verdict: the menu drops down over the details */}
      {open && (
        <div role="menu" aria-label="Flag reason" className="flash-in absolute left-0 top-full z-30 mt-1 w-56 sm:left-auto sm:right-0 overflow-hidden rounded-xl border border-line bg-surface py-1 shadow-md">
          {FLAG_REASONS.map((r) => (
            <button
              key={r}
              role="menuitem"
              type="button"
              onClick={() => {
                setOpen(false);
                onPick(r);
              }}
              className="flex min-h-11 w-full cursor-pointer items-center px-4 text-left text-sm font-medium hover:bg-surface-2"
            >
              {r}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- shipment card -------------------------------------------------------------------------- */

export function ShipmentCard({
  res,
  raw,
  at,
  canUndo,
  flagging,
  onFlag,
  onUndo,
  onNext,
  now,
}: {
  res: ScanResponse;
  raw: string;
  at: number;
  canUndo: boolean;
  flagging: boolean;
  onFlag: (reason: string) => void;
  onUndo: () => void;
  onNext?: () => void;
  now: number;
}) {
  const order = res.order ?? res.scan?.order ?? null;
  const scan = res.scan;
  const saved = !!scan && res.severity !== "error";
  const shape = orderShape(order);
  const cod = !!order && /cod/i.test(order.order_type || "");
  const slaMins = order?.sla_date ? (new Date(order.sla_date).getTime() - now) / 60000 : null;
  const due = order?.dispatch_due;

  const kind = scanKind(res);
  const K = KIND_META[kind];
  const KindIcon = K.icon;
  // the specific reason matters when it is a stop or a check ("Wrong marketplace", "Not packed in OMS")
  const reason = kind === "stop" || kind === "check" ? CODE_TITLE[res.code] ?? res.code : "";
  // server messages start with their own shout ("DUPLICATE - already scanned ..."): the banner title says it already
  const msg = (res.message || "").replace(/^[A-Z][A-Z ]+ - /, "");
  const detail = msg && msg !== "Verified" ? msg.charAt(0).toUpperCase() + msg.slice(1) : K.action;
  const tracking = order?.tracking || scan?.tracking || raw;
  const checks = kind === "check" ? checkReasons(res) : [];
  return (
    <section key={at} className="card shipment-card" aria-label="Scanned shipment">
      {/* verdict + the AWB it is about, in one band */}
      <div className={cx("verdict", `verdict-${kind}`)}>
        <KindIcon className="size-8 shrink-0" aria-hidden />
        <div className="verdict-text">
          <b>
            {K.title}
            {reason && <small> · {reason}</small>}
          </b>
          <span>{detail}</span>
        </div>
        <div className="verdict-awb">
          <span>{order ? "AWB · shipment found" : "Scanned"}</span>
          <b>{tracking}</b>
        </div>
      </div>

      {/* what to watch for (COD, SLA, flags) on the left, what to do on the right */}
      <div className="shipment-bar">
        <div className="shipment-tags">
          {scan && scan.flags.length > 0 && <FlagChips flags={scan.flags} />}
          {order &&
            (cod ? (
              <span className="tag bg-cod-wash text-cod">COD · collect {fmtMoney(order.total_amount, order.currency || "INR")}</span>
            ) : (
              order.order_type && <span className="tag bg-surface-2 text-ink-2">{order.order_type}</span>
            ))}
          {due?.state === "overdue" && (
            <span className="tag bg-crit-wash text-crit-ink" title={`AWB generated ${due.age_days} day${due.age_days > 1 ? "s" : ""} ago - it should have shipped that day`}>
              Overdue · {due.age_days} day{due.age_days > 1 ? "s" : ""}
            </span>
          )}
          {slaMins !== null && slaMins < 0 && <span className="tag bg-crit-wash text-crit-ink">SLA passed</span>}
          {slaMins !== null && slaMins >= 0 && slaMins < 180 && <span className="tag bg-warn-wash text-warn-ink">Priority dispatch · {fmtMins(slaMins)} to SLA</span>}
          {due?.state === "today" && <span className="tag bg-accent-wash text-accent-ink">AWB generated today</span>}
        </div>
        <div className="shipment-actions">
          {saved && <FlagMenu onPick={onFlag} busy={flagging} />}
          {saved && canUndo && (
            <Button variant="ghost" size="sm" onClick={onUndo} title="Undo this scan - the packet can then be scanned again">
              <Undo2 className="size-4" aria-hidden /> Undo
            </Button>
          )}
          {/* the phone camera screen has no Next: the next scan simply replaces this card */}
          {onNext && (
            <Button variant="primary" size="sm" onClick={onNext}>
              Next shipment <ArrowRight className="size-4" aria-hidden />
            </Button>
          )}
        </div>
      </div>

      {kind === "check" && checks.length > 0 && (
        <div className="check-help" role="note" aria-label="What to check">
          <b>
            <AlertTriangle className="size-4 shrink-0" aria-hidden /> What to check
          </b>
          <ul>
            {checks.map((c) => (
              <li key={c}>
                {checks.length > 1 && <span>{FLAG_LABELS[c] ?? c}: </span>}
                {CHECK_HELP[c].todo}
              </li>
            ))}
          </ul>
        </div>
      )}

      {order ? (
        <div className={cx("shipment-body", (order.items.length > 0 || shape.multi) && "with-items")}>
          <dl className="detail-grid">
            <Detail k="Order ID">
              <span className="truncate font-mono" title={order.channel_order_id}>
                {order.channel_order_id}
              </span>
            </Detail>
            <Detail k="Marketplace" sub={order.company}>
              <span className="truncate" title={order.channel_label}>
                {order.channel_label}
              </span>
            </Detail>
            <Detail k="Customer" sub={[order.buyer_city, order.buyer_state, order.buyer_pincode].filter(Boolean).join(", ")}>
              <span className="truncate">{order.buyer_name}</span>
            </Detail>
            <Detail k="Logistics" sub={order.warehouse ? `From ${order.warehouse}` : undefined}>
              <Truck className="size-4 shrink-0 text-muted" aria-hidden />
              <span className="truncate">{order.courier}</span>
            </Detail>
            <Detail k="Invoice" sub={fmtDateTime(order.invoice_date)}>
              <span className="truncate font-mono text-sm">{order.invoice_id}</span>
            </Detail>
            <Detail k="OMS status">
              <span className={cx("truncate", /cancel|return/i.test(order.status_text) && "text-crit-ink")}>{order.status_text || order.status_group}</span>
            </Detail>
          </dl>

          {(order.items.length > 0 || shape.multi) && (
            <div className="items-block">
              {/* multi-item orders turn the items heading into the amber "careful" warning - same line, no extra box */}
              <div className={cx("items-head", shape.multi && "multi")} role={shape.multi ? "note" : undefined}>
                {shape.multi && <AlertTriangle className="size-4 shrink-0" aria-hidden />}
                <h3>{shape.multi ? "Careful - multi-item shipment" : "Items in this shipment"}</h3>
                <span>
                  {shape.multi
                    ? `${shape.skus} SKU${shape.skus > 1 ? "s" : ""} · ${shape.units} unit${shape.units > 1 ? "s" : ""} - pack every item`
                    : `${shape.units} unit${shape.units !== 1 ? "s" : ""}`}
                </span>
              </div>
              {order.items.length > 0 && (
                <div className="sku-list">
                  {order.items.map((it, i) => (
                    <div className="sku-row" key={`${it.sku}-${i}`}>
                      <div className={cx("product-thumb relative overflow-hidden shrink-0", `thumb-${i % 3}`)} aria-hidden>
                        {it.image_url ? (
                          <img
                            src={it.image_url}
                            alt={it.title || it.sku}
                            className="h-full w-full object-cover"
                            onError={(e) => {
                              (e.currentTarget as HTMLElement).style.display = "none";
                              const fallback = e.currentTarget.parentElement?.querySelector(".thumb-fallback") as HTMLElement | null;
                              if (fallback) fallback.style.display = "flex";
                            }}
                          />
                        ) : null}
                        <span className={cx("thumb-fallback h-full w-full items-center justify-center font-bold", it.image_url ? "hidden" : "flex")}>
                          {skuInitials(it.sku)}
                        </span>
                      </div>
                      <div className="sku-main min-w-0">
                        <b className="truncate" title={it.sku}>
                          {it.sku}
                        </b>
                        <span className="truncate" title={it.title || undefined}>
                          {[it.title, it.sub_order_id].filter(Boolean).join(" · ")}
                          {it.status && (
                            <>
                              {" · "}
                              <span className={cx(/cancel|return/i.test(it.status) && "font-semibold text-crit-ink")}>{it.status}</span>
                            </>
                          )}
                        </span>
                      </div>
                      <div className={cx("quantity shrink-0", it.qty > 1 && "many")}>
                        <span>Qty</span>
                        <b>{it.qty}</b>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <p className="shipment-empty">
          {res.code === "NOT_IN_OMS"
            ? "Saved. OMSGuru is being checked now - this shipment turns green (or raises an alert) automatically once the order syncs."
            : res.severity === "error"
              ? "Not saved. Put this packet aside for a supervisor."
              : "No order details for this barcode."}
        </p>
      )}
    </section>
  );
}

/* ---- order journey + SLA + courier ----------------------------------------------------------- */

export function JourneyCard({ res, ctx, now }: { res: ScanResponse; ctx: ScanContext | null; now: number }) {
  const order = res.order ?? res.scan?.order ?? null;
  // a duplicate carries the earlier (saved) scan: the journey shows that one
  const scan = res.scan && (res.severity !== "error" || res.code === "DUPLICATE") ? res.scan : undefined;
  const shipped = order?.status_group === "SHIPPED";
  const steps: { name: string; time: string; state: "done" | "current" | "todo" }[] = [
    { name: "Order received", time: order?.order_date ? fmtDateTime(order.order_date) : "-", state: order?.order_date ? "done" : "todo" },
    {
      name: "AWB generated",
      time: order ? fmtDateTime(order.awb_generated_at ?? order.invoice_date) : "-",
      state: order?.awb_generated_at || order?.invoice_date ? "done" : "todo",
    },
    {
      name: "Forward scan",
      time: scan
        ? `${res.code === "DUPLICATE" ? "Earlier scan " : ""}${scan.scanned_at_local} · ${scan.user}`
        : res.severity === "error"
          ? "Stopped - not scanned"
          : "Not scanned",
      state: scan ? "done" : "current",
    },
    {
      name: "Handover to courier",
      time: shipped ? `OMSGuru: ${order?.status_text || "shipped"}` : scan ? "Waiting for courier pickup" : "Pending",
      state: shipped ? "done" : scan ? "current" : "todo",
    },
  ];
  const slaMins = order?.sla_date ? (new Date(order.sla_date).getTime() - now) / 60000 : null;
  const courier = order?.courier || "";
  const left = courier ? (ctx?.pending_by_courier[courier] ?? 0) : 0;
  const pct = ctx?.awb.pct ?? null;

  return (
    <aside className="timeline-card card" aria-labelledby="journey-title">
      <h3 id="journey-title" className="journey-title">
        Order journey
      </h3>
      <div className="timeline">
        {steps.map((s, i) => (
          <div className={cx("timeline-item", s.state === "done" && "done", s.state === "current" && "current")} key={s.name}>
            <i>{s.state === "done" ? <Check className="size-3" /> : i + 1}</i>
            <div>
              <b>{s.name}</b>
              <span>{s.time}</span>
            </div>
          </div>
        ))}
      </div>

      {/* courier + pickup cutoff in one box (the courier name is also under Logistics in the card) */}
      {order && (
        <div className="cutoff-box">
          <span>Pickup cutoff{courier ? ` · ${courier}` : ""}</span>
          <b>{slaMins === null ? "No SLA from OMSGuru" : slaMins >= 0 ? `${fmtMins(slaMins)} remaining` : `Passed ${fmtMins(slaMins)} ago`}</b>
          <p>{courier ? `${left} ${courier} order${left !== 1 ? "s" : ""} still pending` : "Courier not set in OMSGuru"}</p>
          <div>
            <i style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
          </div>
        </div>
      )}
    </aside>
  );
}
