import { AlertOctagon, ChevronsLeft, ChevronsRight, CircleHelp, KeyRound, Timer, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { api, qs } from "../api";
import { isSupervisor, useAuth } from "../App";
import { useLive, useLiveStatus, useThrottled } from "../live";
import { ChangePasswordDialog } from "./ChangePassword";
import { Icon, useResolvedDark } from "./icons";
import { cx, OutcomePill, ThemeToggle } from "./ui";

/* ---- page titles (the header carries the page's only h1) ------------------------------------- */

const TITLES: [RegExp, string, string][] = [
  [/^\/dashboard/, "Operations dashboard", "Monitor your daily fulfilment performance"],
  [/^\/scan(\/|$)/, "Forward scan", "Scan and verify shipments before dispatch"],
  [/^\/pending/, "Pending & reconciliation", "AWBs generated in OMSGuru vs shipments scanned"],
  [/^\/marketplaces/, "Marketplaces", "Manage connected sales channels and sync health"],
  [/^\/scans/, "Reports", "Export and review your fulfilment data"],
  [/^\/admin/, "Admin control center", "Monitor system health, integrations and access"],
];

const NAV = [
  { to: "/dashboard", label: "Dashboard", icon: "grid", sup: false },
  { to: "/scan", label: "Forward Scan", icon: "scan", sup: false, kbd: "F2" },
  { to: "/marketplaces", label: "Marketplaces", icon: "store", sup: false },
  { to: "/scans", label: "Reports", icon: "report", sup: false },
  { to: "/admin", label: "Admin", icon: "admin", sup: true },
];

const OPS = [
  { to: "/pending", label: "Pending", icon: "box" },
];

export function initials(name: string) {
  return (
    name
      .split(/[\s._-]+/)
      .filter(Boolean)
      .map((p) => p[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "?"
  );
}

export function BrandMark({ size = 22 }: { size?: number }) {
  return (
    <span className="brand-mark" aria-hidden>
      <Icon name="scan" size={size} />
    </span>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <BrandMark />
      <span className="brand-text">
        Forward<span>Scan</span>
      </span>
    </div>
  );
}

function Workspace() {
  const [b, setB] = useState<{ mode: string; warehouses?: string[] } | null>(null);
  useEffect(() => {
    api<{ mode: string; warehouses?: string[] }>("/api/sync/brief").then(setB).catch(() => setB(null));
  }, []);
  const wh = b?.warehouses?.length ? b.warehouses.join(", ") : "ForwardScan HQ";
  const live = useLiveStatus();
  return (
    <div className="workspace">
      <div className="workspace-logo">FS</div>
      <div>
        <b title={wh}>{wh}</b>
        <span>{b?.mode === "mock" ? "Demo data" : live ? "Enterprise workspace" : "Sync reconnecting"}</span>
      </div>
      <Icon name="chevron" size={16} />
    </div>
  );
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  const { user } = useAuth();
  const loc = useLocation();
  const item = (n: { to: string; label: string; icon: string; kbd?: string }) => {
    const on = n.to === "/scan" ? loc.pathname.startsWith("/scan") : loc.pathname === n.to || loc.pathname.startsWith(n.to + "/");
    return (
      <NavLink key={n.to} to={n.to} onClick={onNavigate} className={cx("nav-item", on && "active")} aria-current={on ? "page" : undefined} title={n.label}>
        <Icon name={n.icon} size={19} />
        <span>{n.label}</span>
        {n.kbd && <kbd>{n.kbd}</kbd>}
      </NavLink>
    );
  };
  return (
    <nav aria-label="Primary navigation">
      <span className="nav-label">Workspace</span>
      {NAV.filter((n) => !n.sup || isSupervisor(user)).map(item)}
      <span className="nav-label" style={{ marginTop: 14 }}>
        Operations
      </span>
      {OPS.map(item)}
    </nav>
  );
}

function HelpCard({ onOpen }: { onOpen: () => void }) {
  return (
    <div className="help-card">
      <div className="help-icon">?</div>
      <b>Need help?</b>
      <p>Get support with scanner setup or order issues.</p>
      <button type="button" onClick={onOpen}>
        Contact support
      </button>
    </div>
  );
}

function UserRow() {
  const { user, logout } = useAuth();
  const [changing, setChanging] = useState(false);
  const [changed, setChanged] = useState(false);
  const name = user?.full_name || user?.username || "";
  const role = user?.role === "admin" ? "Operations Admin" : user?.role === "supervisor" ? "Warehouse Manager" : "Scan Operator";
  useEffect(() => {
    if (!changed) return;
    const t = window.setTimeout(() => setChanged(false), 3500);
    return () => window.clearTimeout(t);
  }, [changed]);
  return (
    <div className="user-row">
      <div className="avatar" title={name}>
        {initials(name)}
      </div>
      <div className="user-text min-w-0">
        <b>{name}</b>
        <span>{changed ? "Password changed" : role}</span>
      </div>
      <button type="button" className="logout-button" onClick={() => setChanging(true)} aria-label="Change password" title="Change password">
        <KeyRound className="size-[18px]" aria-hidden />
      </button>
      <button type="button" className="logout-button" onClick={() => void logout()} aria-label="Sign out" title="Sign out">
        <Icon name="logout" size={18} />
      </button>
      {changing && <ChangePasswordDialog onClose={() => setChanging(false)} onChanged={() => setChanged(true)} />}
    </div>
  );
}

function SidebarBody({ onNavigate, onHelp }: { onNavigate?: () => void; onHelp: () => void }) {
  return (
    <>
      <div>
        <Brand />
        <Workspace />
        <NavItems onNavigate={onNavigate} />
      </div>
      <div className="sidebar-bottom">
        <HelpCard onOpen={onHelp} />
        {/* collapsed rail: the help card shrinks to this button */}
        <button type="button" className="help-mini" onClick={onHelp} aria-label="Scanner guide" title="Scanner guide">
          <CircleHelp className="size-5" aria-hidden />
        </button>
        <UserRow />
      </div>
    </>
  );
}

/* ---- notifications: real alerts, stopped scans and overdue AWBs -------------------------------- */

interface Note {
  id: string;
  at: string;
  kind: "alert" | "stopped" | "overdue";
  title: string;
  text: string;
  outcome?: string;
  to: string;
}

function Notifications() {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [notes, setNotes] = useState<Note[]>([]);
  const [seen, setSeen] = useState<number>(() => {
    try {
      return Number(localStorage.getItem("fs_notes_seen") || 0);
    } catch {
      return 0;
    }
  });
  const box = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    Promise.all([
      api<{ scans: { id: number; tracking: string; alert: string; scanned_at: string; channel_name: string }[] }>(`/api/scans${qs({ alerts_only: true, page_size: 6 })}`),
      api<{ events: { id: number; created_at: string; tracking: string; outcome: string; message: string; channel: string }[] }>(
        `/api/events${qs({ outcome: "DUPLICATE,WRONG_CHANNEL,BLOCKED,FLAGGED", limit: 6 })}`,
      ),
      api<{ totals: { overdue: number } }>("/api/reconciliation"),
    ])
      .then(([a, e, r]) => {
        const list: Note[] = [
          ...a.scans.map((s) => ({ id: `a${s.id}`, at: s.scanned_at, kind: "alert" as const, title: `Alert on ${s.tracking}`, text: s.alert.replace("AFTER SCAN: ", ""), to: `/scans?q=${s.tracking}` })),
          ...e.events.map((ev) => ({ id: `e${ev.id}`, at: ev.created_at, kind: "stopped" as const, title: ev.tracking, text: ev.message, outcome: ev.outcome, to: "/scans" })),
        ].sort((x, y) => (x.at < y.at ? 1 : -1));
        if (r.totals.overdue > 0)
          list.unshift({ id: "overdue", at: new Date().toISOString(), kind: "overdue", title: `${r.totals.overdue.toLocaleString("en-IN")} overdue AWBs`, text: "AWB generated on an earlier day and still not dispatched", to: "/pending?bucket=overdue" });
        setNotes(list.slice(0, 10));
      })
      .catch(() => {});
  }, []);

  useEffect(load, [load]);
  const refreshSoon = useThrottled(load, 1500);
  useLive((event) => {
    if (!["scan_rejected", "scan_updated", "sync"].includes(event)) return;
    refreshSoon();
  });
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

  const unread = notes.filter((n) => n.kind !== "overdue" && new Date(n.at).getTime() > seen).length;
  const toggle = () => {
    setOpen((v) => !v);
    const now = Date.now();
    setSeen(now);
    try {
      localStorage.setItem("fs_notes_seen", String(now));
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        onClick={toggle}
        className={cx("icon-button notification", unread === 0 && "[&>i]:hidden")}
        aria-label={unread ? `Notifications, ${unread} new` : "Notifications"}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <Icon name="bell" />
        <i />
      </button>
      {open && (
        <div role="dialog" aria-label="Notifications" className="flash-in absolute right-0 top-12 z-40 w-[min(380px,calc(100vw-32px))] overflow-hidden rounded-xl border border-line bg-surface shadow-md">
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <span className="text-[15px] font-bold">Notifications</span>
            <span className="text-xs text-muted">alerts, stopped scans, overdue</span>
          </div>
          {notes.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-muted">Nothing needs attention right now.</p>
          ) : (
            <ul className="max-h-[60vh] divide-y divide-line overflow-y-auto">
              {notes.map((n) => (
                <li key={n.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setOpen(false);
                      nav(n.to);
                    }}
                    className="ease-ui flex w-full cursor-pointer items-start gap-3 px-4 py-3 text-left hover:bg-surface-2"
                  >
                    <span
                      className={cx(
                        "mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg",
                        n.kind === "overdue" ? "bg-warn-wash text-warn-ink" : "bg-crit-wash text-crit-ink",
                      )}
                      aria-hidden
                    >
                      {n.kind === "overdue" ? <Timer className="size-4" /> : <AlertOctagon className="size-4" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[13px] font-semibold">{n.title}</span>
                        {n.outcome && <OutcomePill outcome={n.outcome} />}
                      </span>
                      <span className="mt-0.5 line-clamp-2 block text-xs text-muted">{n.text}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/* ---- scanner guide -------------------------------------------------------------------------- */

function HelpModal({ onClose }: { onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const rows: [string, ReactNode][] = [
    ["USB / Bluetooth scanner", "Works like a keyboard. Set it to send Enter after each barcode, then click the scan box once - the green \"Scanner ready\" badge confirms it is listening."],
    ["Phone camera", "On a phone, tap Camera: the camera stays open in the top half and each result appears below it - just move from label to label (torch and bigger-camera buttons are on the camera). The Forward Scan Android app works on any phone; in a browser use Chrome on Android over https://. Tap Manual to type a number or use a Bluetooth scanner."],
    ["Amber = Check", "The scan is saved, but something needs a person (not packed in OMS, partly cancelled, no longer Ready to ship). The amber \"What to check\" box on the result says exactly what to do. Shipped / In Transit in OMS is normal and scans green."],
    ["Colours & sounds", "Green + 1 beep = OK, put it in the bag. Blue + 3 quick beeps = duplicate, already scanned - set aside. Purple + high-low tone = not found in OMSGuru yet, saved as unverified. Amber + 2 beeps = saved, but check the packet. Red + buzzer = stop (wrong marketplace, cancelled, invalid) - put it aside and press Enter. Tap the legend on the scan page to hear each sound."],
    ["Multi-item shipments", "An amber \"Careful - multi-item shipment\" box lists every SKU and unit. Check all items are inside before bagging it; use Flag issue if something is missing."],
    ["Wrong account or password", "Ask your supervisor or admin - they reset passwords and add users in Admin."],
  ];
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-[rgba(4,12,8,0.58)] p-4 backdrop-blur-sm" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="help-title" className="card flash-in w-full max-w-lg p-6" onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <span className="eyebrow">Help</span>
            <h2 id="help-title" className="mt-1 text-xl font-bold">
              Scanner guide
            </h2>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} className="grid size-10 cursor-pointer place-items-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close guide">
            <X className="size-5" aria-hidden />
          </button>
        </div>
        <dl className="mt-4 space-y-4">
          {rows.map(([k, v]) => (
            <div key={k}>
              <dt className="text-sm font-bold">{k}</dt>
              <dd className="mt-0.5 text-sm leading-6 text-ink-2">{v}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

/* ---- shell ---------------------------------------------------------------------------------- */

export function Shell({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const dark = useResolvedDark();
  const [menu, setMenu] = useState(false);
  const [help, setHelp] = useState(false);
  // desktop: the sidebar folds to an icon rail (remembered per device); hovering the rail opens it over the page
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("fs_sidebar_collapsed") === "1";
    } catch {
      return false;
    }
  });
  function toggleSidebar() {
    setCollapsed((c) => {
      try {
        localStorage.setItem("fs_sidebar_collapsed", c ? "0" : "1");
      } catch {
        /* storage may be blocked - the choice still applies for this visit */
      }
      return !c;
    });
  }
  const loc = useLocation();
  const nav = useNavigate();
  const [title, subtitle] = (TITLES.find(([re]) => re.test(loc.pathname)) ?? [null, "Forward Scan", ""]).slice(1) as [string, string];

  useEffect(() => setMenu(false), [loc.pathname]);
  useEffect(() => {
    document.title = `${title} · ForwardScan`;
  }, [title]);
  // F2 jumps to Forward Scan from anywhere (design shortcut); Esc closes the phone menu.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "F2") {
        e.preventDefault();
        let last = "";
        try {
          last = localStorage.getItem("fs_last_channel") || "";
        } catch {
          /* ignore */
        }
        nav(last ? `/scan/${last}` : "/scan");
      } else if (e.key === "Escape") setMenu(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav]);

  return (
    <div className={cx("app fs-shell", dark && "dark", collapsed && "collapsed")}>
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[70] focus:rounded-lg focus:bg-accent focus:px-4 focus:py-2 focus:text-on-accent">
        Skip to content
      </a>
      <aside className={cx("sidebar", menu && "sidebar-open")}>
        <SidebarBody onNavigate={() => setMenu(false)} onHelp={() => setHelp(true)} />
        <button
          type="button"
          className="sidebar-toggle"
          onClick={toggleSidebar}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-pressed={collapsed}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <ChevronsRight className="size-4" aria-hidden /> : <ChevronsLeft className="size-4" aria-hidden />}
        </button>
      </aside>
      {menu && <div className="fixed inset-0 z-20 lg:hidden" onClick={() => setMenu(false)} aria-hidden />}

      <main>
        <header>
          <button className="mobile-menu" type="button" onClick={() => setMenu((v) => !v)} aria-label="Toggle menu" aria-expanded={menu}>
            <span />
            <span />
            <span />
          </button>
          <div className="page-title">
            <h1>{title}</h1>
            <p>{subtitle}</p>
          </div>
          <div className="header-actions">
            <ThemeToggle />
            <Notifications />
            <div className="header-avatar" title={user?.full_name || user?.username}>
              {initials(user?.full_name || user?.username || "")}
            </div>
          </div>
        </header>
        <div className="content" id="main">
          {children}
        </div>
      </main>
      {help && <HelpModal onClose={() => setHelp(false)} />}
    </div>
  );
}
