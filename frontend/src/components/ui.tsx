import { AlertOctagon, AlertTriangle, CheckCircle2, CircleHelp, Copy, Flag, Inbox, Loader2, Monitor, X } from "lucide-react";
import { Icon } from "./icons";
import { useEffect, useState, type ButtonHTMLAttributes, type ComponentType, type ReactNode } from "react";
import { FLAG_LABELS } from "../api";
import { getTheme, setTheme, type ThemeChoice } from "../theme";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

/* ---- buttons: 44px default (touch), 36px compact, 48px for scan-station actions ------------- */

type BtnVariant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  size = "md",
  loading,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: "sm" | "md" | "lg"; loading?: boolean }) {
  const v = {
    primary: "bg-accent text-on-accent border-transparent font-semibold hover:bg-accent-hover",
    secondary: "bg-surface text-ink border-line-strong hover:bg-surface-2",
    ghost: "bg-transparent text-ink-2 border-transparent hover:bg-surface-2 hover:text-ink",
    danger: "bg-crit text-white border-transparent hover:brightness-110",
  }[variant];
  const s = { sm: "h-9 px-3 text-sm", md: "h-11 px-4 text-sm", lg: "h-12 px-5 text-base" }[size];
  return (
    <button
      type="button"
      {...rest}
      disabled={rest.disabled || loading}
      aria-busy={loading || undefined}
      className={cx(
        // pressed feedback via opacity only - never shifts layout
        "ease-ui inline-flex cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-lg border font-medium active:opacity-80 disabled:cursor-not-allowed disabled:opacity-50",
        v,
        s,
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

/** Icon-only button: always needs a visible tooltip + accessible name. */
export function IconButton({ label, children, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      {...rest}
      className={cx(
        "ease-ui inline-flex size-10 cursor-pointer items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink active:opacity-80 disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
    >
      {children}
    </button>
  );
}

/* ---- layout --------------------------------------------------------------------------------- */

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cx("card", className)}>{children}</div>;
}

/** Card with a header row: title (+ description) on the left, actions on the right. */
export function SectionCard({
  title,
  description,
  actions,
  children,
  className,
  bodyClass,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
  eyebrow?: ReactNode;
}) {
  return (
    <section className={cx("card overflow-hidden", className)}>
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-line px-5 py-4 sm:px-6">
        <div className="min-w-0">
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2 className="mt-0.5 text-[17px] font-bold leading-6 tracking-tight">{title}</h2>
          {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

/** Page toolbar under the shell header (which carries the page title as the page's h1). */
export function PageHeader({ title, sub, actions, eyebrow }: { title: string; sub?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4" aria-label={title} role="group">
      <div className="min-w-0 max-w-3xl">
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        {sub && <div className="mt-1 text-[15px] leading-6 text-ink-2">{sub}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function ChannelDot({ color, size = 10 }: { color: string; size?: number }) {
  return <span aria-hidden className="inline-block shrink-0 rounded-full" style={{ width: size, height: size, background: color || "#898781" }} />;
}

/* ---- status pills: colour + icon + label, never colour alone -------------------------------- */

const RESULT_META = {
  OK: { label: "Verified", icon: CheckCircle2, cls: "bg-good-wash text-good-ink" },
  WARN: { label: "Check", icon: AlertTriangle, cls: "bg-warn-wash text-warn-ink" },
  UNVERIFIED: { label: "Not found", icon: CircleHelp, cls: "bg-nf-wash text-nf-ink" },
  ALERT: { label: "Alert", icon: AlertOctagon, cls: "bg-crit-wash text-crit-ink" },
} as const;

export function ResultPill({ result, alert }: { result: string; alert?: string }) {
  const key = (alert ? "ALERT" : result) as keyof typeof RESULT_META;
  const m = RESULT_META[key] ?? RESULT_META.WARN;
  const Icon = m.icon;
  return (
    <span className={cx("inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium", m.cls)}>
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {m.label}
    </span>
  );
}

export function FlagChips({ flags }: { flags: string[] }) {
  if (!flags?.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {flags.map((f) => (
        <span key={f} className="whitespace-nowrap rounded-md border border-line px-1.5 py-0.5 text-xs text-ink-2">
          {FLAG_LABELS[f] ?? f}
        </span>
      ))}
    </span>
  );
}

const OUTCOME_META: Record<string, { label: string; cls: string; icon: ComponentType<{ className?: string }> }> = {
  ACCEPTED: { label: "Accepted", cls: "bg-good-wash text-good-ink", icon: CheckCircle2 },
  WARN: { label: "Accepted - check", cls: "bg-warn-wash text-warn-ink", icon: AlertTriangle },
  UNVERIFIED: { label: "Not found - unverified", cls: "bg-nf-wash text-nf-ink", icon: CircleHelp },
  DUPLICATE: { label: "Duplicate", cls: "bg-dup-wash text-dup-ink", icon: Copy },
  WRONG_CHANNEL: { label: "Wrong marketplace", cls: "bg-crit-wash text-crit-ink", icon: AlertOctagon },
  BLOCKED: { label: "Blocked", cls: "bg-crit-wash text-crit-ink", icon: AlertOctagon },
  INVALID: { label: "Invalid", cls: "bg-crit-wash text-crit-ink", icon: AlertOctagon },
  VOIDED: { label: "Removed", cls: "bg-surface-2 text-ink-2", icon: X },
  FLAGGED: { label: "Flagged", cls: "bg-warn-wash text-warn-ink", icon: Flag },
  ERROR: { label: "NOT saved - server error, scan again", cls: "bg-crit-wash text-crit-ink", icon: AlertOctagon },
  NOT_FOUND: { label: "Not found - not saved", cls: "bg-nf-wash text-nf-ink", icon: CircleHelp },
  REPEAT: { label: "Already saved (own repeat)", cls: "bg-good-wash text-good-ink", icon: CheckCircle2 },
};

export function OutcomePill({ outcome }: { outcome: string }) {
  const m = OUTCOME_META[outcome] ?? { label: outcome, cls: "bg-surface-2 text-ink-2", icon: CircleHelp };
  const Icon = m.icon;
  return (
    <span className={cx("inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium", m.cls)}>
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {m.label}
    </span>
  );
}

/* ---- KPI tiles ------------------------------------------------------------------------------ */

const TONE_TEXT = { good: "text-good-ink", warn: "text-warn-ink", crit: "text-crit-ink" } as const;
const TONE_ICON = { good: "bg-good-wash text-good-ink", warn: "bg-warn-wash text-warn-ink", crit: "bg-crit-wash text-crit-ink" } as const;

export function Stat({
  label,
  value,
  tone,
  hint,
  icon: Icon,
  onClick,
}: {
  label: string;
  value: ReactNode;
  tone?: "good" | "warn" | "crit";
  hint?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  onClick?: () => void;
}) {
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm font-medium text-ink-2">{label}</div>
        {Icon && (
          <span className={cx("grid size-8 shrink-0 place-items-center rounded-lg", tone ? TONE_ICON[tone] : "bg-accent-wash text-accent-ink")}>
            <Icon className="size-4" aria-hidden />
          </span>
        )}
      </div>
      <div className={cx("mt-1 text-[28px] font-semibold leading-9 tracking-tight", tone ? TONE_TEXT[tone] : "text-ink")}>{value}</div>
      {hint && <div className="mt-0.5 text-sm text-muted">{hint}</div>}
    </>
  );
  return onClick ? (
    <button type="button" onClick={onClick} className="card ease-ui cursor-pointer p-4 text-left hover:border-line-strong">
      {body}
    </button>
  ) : (
    <div className="card p-4">{body}</div>
  );
}

/* ---- empty / loading ------------------------------------------------------------------------ */

export function Empty({ children, title, icon: Icon = Inbox }: { children?: ReactNode; title?: string; icon?: ComponentType<{ className?: string }> }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <span className="grid size-11 place-items-center rounded-full bg-surface-2 text-muted">
        <Icon className="size-5" aria-hidden />
      </span>
      {title && <div className="text-[15px] font-semibold text-ink">{title}</div>}
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("skeleton", className)} aria-hidden />;
}

/** Placeholder rows that keep the table's height while data loads (no layout shift). */
export function SkeletonRows({ rows = 6, className }: { rows?: number; className?: string }) {
  return (
    <div className={cx("space-y-3 p-4 sm:p-5", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-4">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="hidden h-4 w-24 sm:block" />
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  );
}

export function SkeletonStats({ count = 4 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="card space-y-3 p-4" aria-hidden>
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-20" />
          <Skeleton className="h-3 w-32" />
        </div>
      ))}
    </>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-muted">
      <Loader2 className="size-5 animate-spin" aria-hidden />
      {label}
    </span>
  );
}

/* ---- forms ---------------------------------------------------------------------------------- */

export function Field({ label, children, className, hint, error }: { label: string; children: ReactNode; className?: string; hint?: ReactNode; error?: ReactNode }) {
  return (
    <label className={cx("flex min-w-0 flex-col gap-1.5 text-sm font-medium text-ink-2", className)}>
      {label}
      {children}
      {hint && !error && <span className="text-xs font-normal text-muted">{hint}</span>}
      {error && (
        <span className="text-xs font-medium text-crit-ink" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

// 44px controls; 16px text on phones (no zoom-on-focus), 14px on desktop.
export const inputCls =
  "ease-ui h-11 min-w-0 rounded-lg border border-line-strong bg-surface px-3 text-base text-ink placeholder:text-muted hover:border-ink-2/50 focus:border-accent focus:outline-none focus:ring-2 focus:ring-[var(--ring)] disabled:opacity-60 sm:text-sm";

/* ---- feedback ------------------------------------------------------------------------------- */

export function Toast({ kind, children, onClose }: { kind: "ok" | "err"; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const t = window.setTimeout(onClose, kind === "ok" ? 3500 : 6000);
    return () => window.clearTimeout(t);
    // onClose is usually an inline arrow; restarting the timer on every parent render would keep the toast up forever
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, children]);
  const Icon = kind === "ok" ? CheckCircle2 : AlertOctagon;
  return (
    <div
      role={kind === "ok" ? "status" : "alert"}
      className={cx(
        "flash-in fixed bottom-4 left-4 right-4 z-50 flex items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium text-white shadow-md sm:bottom-auto sm:left-auto sm:right-6 sm:top-24 sm:max-w-md",
        kind === "ok" ? "bg-[#173c2e]" : "bg-[#713e12]",
      )}
    >
      <span className={cx("grid size-6 shrink-0 place-items-center rounded-full", kind === "ok" ? "bg-[#42a97c]" : "bg-[#a86424]")}>
        <Icon className="size-3.5" aria-hidden />
      </span>
      <span>{children}</span>
      <button type="button" onClick={onClose} className="-mr-1 rounded p-1 opacity-80 hover:opacity-100" aria-label="Dismiss">
        <X className="size-4" aria-hidden />
      </button>
    </div>
  );
}

/** "Live · updated 12s ago" - only claims live when the data is actually fresh and the socket is up. */
export function LiveBadge({ updatedAt, connected, staleAfter = 120 }: { updatedAt: number | null; connected: boolean; staleAfter?: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 5000);
    return () => window.clearInterval(t);
  }, []);
  const age = updatedAt ? Math.max(0, Math.round((Date.now() - updatedAt) / 1000)) : null;
  const ago = age === null ? "never" : age < 60 ? `${age}s ago` : age < 3600 ? `${Math.floor(age / 60)} min ago` : `${Math.floor(age / 3600)} h ago`;
  const state = !connected ? "offline" : age === null || age > staleAfter ? "stale" : "live";
  const meta = {
    live: { dot: "bg-good pulse-dot", text: "Live", cls: "text-ink-2" },
    stale: { dot: "bg-warn", text: "Not refreshed", cls: "text-warn-ink" },
    offline: { dot: "bg-crit", text: "Reconnecting", cls: "text-crit-ink" },
  }[state];
  return (
    <span className={cx("inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-xs font-medium", meta.cls)} title={`Last update ${ago}`}>
      <span className={cx("size-2 rounded-full", meta.dot)} aria-hidden />
      {meta.text}
      <span className="font-normal text-muted">· updated {ago}</span>
    </span>
  );
}

/** Header sun / moon button (design). Flips between light and dark and remembers it on this device. */
export function ThemeToggle() {
  const [, force] = useState(0);
  const dark =
    document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.getAttribute("data-theme") && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  return (
    <button
      type="button"
      className="icon-button theme-toggle"
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      onClick={() => {
        setTheme(dark ? "light" : "dark");
        force((n) => n + 1);
      }}
    >
      <Icon name={dark ? "sun" : "moon"} />
    </button>
  );
}

/** System / light / dark - remembered per device (bright warehouse stations usually want Light). */
export function ThemeSwitch({ compact }: { compact?: boolean }) {
  const [choice, setChoice] = useState<ThemeChoice>(getTheme);
  const opts: { v: ThemeChoice; label: string; Icon: ComponentType<{ className?: string }> }[] = [
    { v: "system", label: "Use system theme", Icon: Monitor },
    { v: "light", label: "Light theme", Icon: ({ className }: { className?: string }) => <span className={className}><Icon name="sun" size={16} /></span> },
    { v: "dark", label: "Dark theme", Icon: ({ className }: { className?: string }) => <span className={className}><Icon name="moon" size={16} /></span> },
  ];
  return (
    <div role="radiogroup" aria-label="Theme" className={cx("inline-flex rounded-lg border border-nav-line p-0.5", compact && "scale-95")}>
      {opts.map(({ v, label, Icon }) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={choice === v}
          aria-label={label}
          title={label}
          onClick={() => {
            setTheme(v);
            setChoice(v);
          }}
          className={cx(
            "ease-ui grid size-9 cursor-pointer place-items-center rounded-md",
            choice === v ? "bg-nav-active text-nav-active-ink" : "text-nav-muted hover:text-nav-ink",
          )}
        >
          <Icon className="size-4" aria-hidden />
        </button>
      ))}
    </div>
  );
}
