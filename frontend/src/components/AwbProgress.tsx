import { simpleCounts, type AwbCounts } from "../api";
import { cx } from "./ui";

/** Synced orders as one bar: scanned + pending = synced (owner, 10 Oct 2026: "1000 synced, 980 scanned -> 20
    pending"). Pending includes earlier days' unscanned AWBs and AWBs OMS already shows shipped but nobody scanned here
    (left_unscanned, named next to the legend); cancelled orders are not in it. */
export function AwbProgress({ c, compact, title = "Synced orders", bare }: { c: AwbCounts; compact?: boolean; title?: string; bare?: boolean }) {
  const s = simpleCounts(c);
  const total = Math.max(0, s.synced);
  const segments = [
    { key: "scanned", label: "Scanned", value: s.scanned, color: "var(--good)" },
    { key: "pending", label: "Pending", value: s.pending, color: "var(--warn)" },
  ];
  const label = segments.map((x) => `${x.label} ${x.value}`).join(", ");
  const bar = (h: string) => (
    <div className={cx("flex w-full gap-[2px] overflow-hidden rounded-full bg-surface-2", h)} role="img" aria-label={label}>
      {total > 0 && segments.map((x) => (x.value > 0 ? <div key={x.key} style={{ width: `${(x.value / total) * 100}%`, background: x.color }} /> : null))}
    </div>
  );
  if (bare) {
    // table cell: just the bar and the % scanned (counts are in the neighbouring columns)
    return (
      <div className="flex items-center gap-2">
        <div className="flex-1">{bar("h-1.5")}</div>
        <span className="tnum w-10 text-right text-xs text-ink-2">{c.pct === null ? "-" : `${c.pct}%`}</span>
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <div className="text-xs font-medium text-ink-2">
        {title}: <b className="tnum text-sm text-ink">{total.toLocaleString("en-IN")}</b>
        {c.pct !== null && total > 0 && <span className="ml-2 text-muted">{c.pct}% scanned</span>}
      </div>
      <div className="mt-1.5">{bar(compact ? "h-1.5" : "h-2.5")}</div>
      {!compact && (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
          {segments.map((x) => (
            <span key={x.key} className="inline-flex items-center gap-1.5">
              <span className="inline-block size-2 rounded-full" style={{ background: x.color }} aria-hidden />
              {x.label} <b className="tnum text-ink">{x.value.toLocaleString("en-IN")}</b>
              {x.key === "pending" && c.left_unscanned > 0 && (
                <span className="text-muted">({c.left_unscanned.toLocaleString("en-IN")} already shipped in OMS)</span>
              )}
            </span>
          ))}
          {c.cancelled > 0 && <span className="text-muted">{c.cancelled.toLocaleString("en-IN")} cancelled - not counted</span>}
        </div>
      )}
    </div>
  );
}
