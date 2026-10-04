import { ArrowRight, Undo2, X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ScanResponse } from "../api";
import { CODE_TITLE, KIND_META, scanKind } from "./ShipmentCard";
import { cx } from "./ui";

/* Phone-only verdict bottom sheet: the moment a scan lands, the packer sees the result
   right above the thumb - no scrolling to the card below. Every kind (OK, duplicate,
   not found, check, stop) pops up immediately; the next scan replaces it. Dismiss with
   the primary button, the X, the backdrop, or Escape. Desktop keeps the inline card. */

export interface SheetLast {
  res: ScanResponse;
  raw: string;
  at: number;
}

export function ResultSheet({
  last,
  channelName,
  canUndo,
  onPrimary,
  onDismiss,
  onUndo,
}: {
  last: SheetLast;
  channelName: string;
  canUndo: boolean;
  onPrimary: () => void;
  onDismiss: () => void;
  onUndo: () => void;
}) {
  const kind = scanKind(last.res);
  const K = KIND_META[kind];
  const KindIcon = K.icon;
  const reason = kind === "stop" || kind === "check" ? (CODE_TITLE[last.res.code] ?? last.res.code) : "";
  const msg = (last.res.message || "").replace(/^[A-Z][A-Z ]+ - /, "");
  const detail = msg && msg !== "Verified" ? msg.charAt(0).toUpperCase() + msg.slice(1) : K.action;
  const tracking = last.res.scan?.tracking ?? last.res.order?.tracking ?? last.raw;
  const marketplace = last.res.order?.channel_label ?? (last.res.code === "DUPLICATE" ? last.res.scan?.channel_name : undefined);
  const primary = useRef<HTMLButtonElement>(null);

  // Lock the page behind the sheet; focus the primary action (a button - no keyboard pops up).
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    primary.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [onDismiss]);

  return (
    <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label={`${K.title} - ${tracking}`}>
      <button
        type="button"
        aria-label="Dismiss result"
        onClick={onDismiss}
        className="absolute inset-0 cursor-default bg-black/55"
      />
      <div className="sheet-up absolute inset-x-0 bottom-0 max-h-[82dvh] overflow-y-auto rounded-t-2xl bg-surface shadow-md">
        <div className={cx("flex items-center gap-3 px-5 pb-3 pt-4 text-white", `verdict-${kind}`)} style={{ background: "var(--v)" }}>
          <KindIcon className="size-9 shrink-0" aria-hidden />
          <div className="min-w-0 flex-1">
            <b className="block text-xl font-extrabold uppercase leading-tight tracking-wide">
              {K.title}
              {reason && <small className="ml-1.5 text-sm font-bold normal-case tracking-normal">· {reason}</small>}
            </b>
            <span className="block truncate text-sm opacity-95">{channelName}</span>
          </div>
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss result"
            className="grid size-11 shrink-0 cursor-pointer place-items-center rounded-full bg-white/15 text-white active:opacity-80"
          >
            <X className="size-5" aria-hidden />
          </button>
        </div>
        <div className="px-5 py-4">
          <p className="text-[15px] font-medium leading-snug text-ink">{detail}</p>
          <dl className="mt-3 space-y-1.5 text-sm">
            <div className="flex items-baseline justify-between gap-3">
              <dt className="shrink-0 text-muted">Tracking</dt>
              <dd className="truncate font-mono font-bold text-ink">{tracking}</dd>
            </div>
            {marketplace && (
              <div className="flex items-baseline justify-between gap-3">
                <dt className="shrink-0 text-muted">Marketplace</dt>
                <dd className="truncate font-semibold text-ink">{marketplace}</dd>
              </div>
            )}
          </dl>
          <div className="mt-4 grid gap-2 pb-[env(safe-area-inset-bottom)]">
            <button
              ref={primary}
              type="button"
              onClick={onPrimary}
              className="ease-ui flex min-h-13 cursor-pointer items-center justify-center gap-2 rounded-xl bg-accent px-5 py-3.5 text-base font-bold text-on-accent active:opacity-80"
            >
              Next shipment <ArrowRight className="size-5" aria-hidden />
            </button>
            {canUndo && (
              <button
                type="button"
                onClick={onUndo}
                className="ease-ui flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-xl border border-line-strong bg-surface px-4 py-2.5 text-sm font-semibold text-ink-2 active:opacity-80"
              >
                <Undo2 className="size-4" aria-hidden /> Undo this scan
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
