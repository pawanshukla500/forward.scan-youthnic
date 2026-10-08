import type { AwbCounts } from "../api";
import { cx } from "./ui";

// Status colours (fixed, never reused for series); every segment is also named in the legend with its count.
const SEGMENTS: { key: keyof AwbCounts; label: string; color: string }[] = [
  { key: "scanned", label: "Scanned", color: "var(--good)" },
  { key: "pending", label: "Pending", color: "var(--warn)" },
  { key: "cancelled", label: "Cancelled after AWB", color: "var(--line-strong)" },
];

/** Stacked bar of one day's AWBs (scanned / pending / cancelled) + overdue from earlier days. Pending includes AWBs OMS
    already shows shipped but nobody scanned here (left_unscanned, named next to the legend). */
export function AwbProgress({ c, compact, title = "AWBs generated today", bare }: { c: AwbCounts; compact?: boolean; title?: string; bare?: boolean }) {
  const total = Math.max(0, c.generated);
  if (bare) {
    // table cell: just the bar and the % scanned (counts are in the neighbouring columns)
    return (
      <div className="flex items-center gap-2">
        <div className="flex h-1.5 flex-1 gap-[2px] overflow-hidden rounded-full bg-surface-2" role="img"
             aria-label={SEGMENTS.map((s) => `${s.label} ${c[s.key] ?? 0}`).join(", ")}>
          {total > 0 && SEGMENTS.map((s) => {
            const v = Number(c[s.key] ?? 0);
            return v > 0 ? <div key={s.key} style={{ width: `${(v / total) * 100}%`, background: s.color }} /> : null;
          })}
        </div>
        <span className="tnum w-10 text-right text-xs text-ink-2">{c.pct === null ? "-" : `${c.pct}%`}</span>
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="text-xs font-medium text-ink-2">
          {title}: <b className="tnum text-sm text-ink">{total.toLocaleString("en-IN")}</b>
          {c.pct !== null && total > 0 && <span className="ml-2 text-muted">{c.pct}% scanned</span>}
        </div>
        {c.overdue > 0 && (
          <span className="rounded-full bg-crit-wash px-2 py-0.5 text-xs font-semibold text-crit-ink">
            {c.overdue.toLocaleString("en-IN")} overdue from earlier days
          </span>
        )}
      </div>
      <div
        className={cx("mt-1.5 flex w-full gap-[2px] overflow-hidden rounded-full bg-surface-2", compact ? "h-1.5" : "h-2.5")}
        role="img"
        aria-label={SEGMENTS.map((s) => `${s.label} ${c[s.key] ?? 0}`).join(", ")}
      >
        {total > 0 &&
          SEGMENTS.map((s) => {
            const v = Number(c[s.key] ?? 0);
            return v > 0 ? <div key={s.key} style={{ width: `${(v / total) * 100}%`, background: s.color }} /> : null;
          })}
      </div>
      {!compact && (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
          {SEGMENTS.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span className="inline-block size-2 rounded-full" style={{ background: s.color }} aria-hidden />
              {s.label} <b className="tnum text-ink">{Number(c[s.key] ?? 0).toLocaleString("en-IN")}</b>
              {s.key === "pending" && c.left_unscanned > 0 && (
                <span className="text-muted">({c.left_unscanned.toLocaleString("en-IN")} already shipped in OMS)</span>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
