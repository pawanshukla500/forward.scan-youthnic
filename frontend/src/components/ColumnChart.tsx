import { useMemo, useRef, useState } from "react";

export interface ColumnDatum {
  key: string;
  label: string; // axis label
  tip: string; // tooltip heading
  value: number;
}

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}

/** Single-series column chart: thin bars, rounded data-end, hairline grid, hover tooltip, table fallback. */
export function ColumnChart({
  data,
  height = 180,
  unit = "shipments",
  labelEvery = 1,
  highlightKey,
  caption,
}: {
  data: ColumnDatum[];
  height?: number;
  unit?: string;
  labelEvery?: number;
  highlightKey?: string;
  caption: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const max = useMemo(() => niceMax(Math.max(0, ...data.map((d) => d.value))), [data]);
  const ticks = [0, max / 2, max];
  const padL = 40;
  const padB = 22;
  const padT = 8;
  const W = 720;
  const H = height;
  const plotW = W - padL - 4;
  const plotH = H - padB - padT;
  const band = plotW / Math.max(1, data.length);
  const barW = Math.min(24, Math.max(4, band - 2));
  const y = (v: number) => padT + plotH - (v / max) * plotH;

  const hovered = hover !== null ? data[hover] : null;

  return (
    <figure className="relative" ref={wrap}>
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label={caption} onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={W - 4} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth={1} />
            <text x={padL - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill="var(--muted)" className="tnum">
              {Math.round(t).toLocaleString("en-IN")}
            </text>
          </g>
        ))}
        <line x1={padL} x2={W - 4} y1={y(0)} y2={y(0)} stroke="var(--line-strong)" strokeWidth={1} />
        {data.map((d, i) => {
          const cx = padL + band * i + band / 2;
          const h = Math.max(0, y(0) - y(d.value));
          const r = Math.min(4, h, barW / 2);
          const x0 = cx - barW / 2;
          const top = y(0) - h;
          const path =
            h <= 0
              ? ""
              : `M${x0},${y(0)} L${x0},${top + r} Q${x0},${top} ${x0 + r},${top} L${x0 + barW - r},${top} Q${x0 + barW},${top} ${x0 + barW},${top + r} L${x0 + barW},${y(0)} Z`;
          const dim = hover !== null && hover !== i;
          return (
            <g key={d.key}>
              {path && (
                <path
                  d={path}
                  fill="var(--series-1)"
                  opacity={dim ? 0.45 : highlightKey && highlightKey !== d.key && hover === null ? 0.75 : 1}
                />
              )}
              {i % labelEvery === 0 && (
                <text x={cx} y={H - 6} textAnchor="middle" fontSize={11} fill="var(--muted)">
                  {d.label}
                </text>
              )}
              {/* hit target wider than the mark */}
              <rect x={padL + band * i} y={padT} width={band} height={plotH} fill="transparent" onMouseEnter={() => setHover(i)} />
            </g>
          );
        })}
      </svg>
      {hovered && hover !== null && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-lg border border-line bg-surface px-3 py-2 text-xs shadow-md"
          style={{ left: `${((padL + band * hover + band / 2) / W) * 100}%`, top: 0 }}
        >
          <div className="font-medium text-ink">{hovered.tip}</div>
          <div className="tnum text-ink-2">
            {hovered.value.toLocaleString("en-IN")} {unit}
          </div>
        </div>
      )}
      <figcaption className="sr-only">{caption}</figcaption>
      <table className="sr-only">
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th>Period</th>
            <th>{unit}</th>
          </tr>
        </thead>
        <tbody>
          {data.map((d) => (
            <tr key={d.key}>
              <td>{d.tip}</td>
              <td>{d.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
