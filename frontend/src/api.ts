export type Role = "admin" | "supervisor" | "scanner";

export interface User {
  id: number;
  username: string;
  full_name: string;
  email: string;
  role: Role;
  /** an admin created or reset this password: the person must choose their own before anything else */
  must_change_password: boolean;
  password_changed_at: string | null;
}

/** Same rule as the server (security.password_problem). */
export const PASSWORD_RULE = "At least 8 characters, with a letter and a number";
export function passwordProblem(pw: string): string | null {
  if (pw.length < 8 || !/[A-Za-z]/.test(pw) || !/\d/.test(pw)) return PASSWORD_RULE;
  return null;
}

export interface Channel {
  id: number;
  name: string;
  marketplace: string;
  company: string;
  color: string;
  oms_status: string;
  scan_enabled: boolean;
  sort_order: number;
  today?: number;
  pending?: number;
  awb_today?: AwbCounts;
}

/** AWBs generated for a channel on a day, reconciled against scans. */
export interface AwbCounts {
  generated: number;
  scanned: number;
  pending: number;
  overdue: number;
  left_unscanned: number;
  cancelled: number;
  pct: number | null;
}

export interface OrderItem {
  sku: string;
  qty: number;
  sub_order_id: string;
  status: string;
  amount: number;
  title?: string | null;
  image_url?: string | null;
}

export interface Order {
  id: number;
  channel_label: string;
  channel_id: number | null;
  company: string;
  warehouse: string;
  channel_order_id: string;
  sub_order_ids: string[];
  invoice_id: string;
  invoice_date: string | null;
  order_date: string | null;
  sla_date: string | null;
  tracking: string;
  courier: string;
  order_type: string;
  buyer_name: string;
  buyer_city: string;
  buyer_state: string;
  buyer_pincode: string;
  item_count: number;
  total_qty: number;
  total_amount: number;
  currency: string;
  items: OrderItem[];
  status_text: string;
  status_group: string;
  synced_at: string | null;
  awb_generated_at?: string | null;
  dispatch_due?: { state: "today" | "overdue"; age_days: number } | null;
}

export type ScanResult = "OK" | "WARN" | "UNVERIFIED";

export interface Scan {
  id: number;
  tracking: string;
  tracking_norm: string;
  dispatch_date: string;
  scanned_at: string;
  scanned_at_local: string;
  user: string;
  user_id: number;
  station: string;
  channel_id: number;
  channel_name: string;
  marketplace: string;
  result: ScanResult;
  flags: string[];
  message: string;
  alert: string;
  manifest_id: number | null;
  manifest?: { status: "OPEN" | "CLOSED" | string; closed_at: string | null; number: string } | null;
  oms_update_status: string;
  order: Order | null;
}

export type Severity = "success" | "warning" | "error";

export type Priority = "Urgent" | "High" | "Normal";

export interface QueueRow {
  awb: string;
  order_id: string;
  courier: string;
  skus: number;
  units: number;
  sla_date: string | null;
  awb_generated_at: string | null;
  age_days: number;
  priority: Priority;
}

/** Everything the scan page shows around the scan box (GET /api/scan-context). */
export interface ScanContext {
  date: string;
  channel: Channel;
  stats: { scanned: number; ok: number; flagged: number; flagged_manual: number; alerts: number; rejected: number; yesterday: number };
  awb: AwbCounts;
  queue: QueueRow[];
  queue_total: number;
  pending_by_courier: Record<string, number>;
  server_time: string;
}

export const FLAG_REASONS = ["Missing item", "Wrong item", "Damaged packet", "Label problem", "Weight mismatch", "Other"] as const;

export interface ScanResponse {
  severity: Severity;
  code: string;
  message: string;
  scan?: Scan;
  order?: Order | null;
  /** fresh = fetched from OMSGuru during this scan; busy/timeout/error = checked against the local copy */
  live?: "fresh" | "unconfirmed" | "not_in_oms" | "busy" | "timeout" | "error" | "off" | null;
  live_ms?: number | null;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(path, {
    credentials: "same-origin",
    ...rest,
    headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...(headers || {}) },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/login")) onUnauthorized?.();
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join("; ") : msg;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg || `HTTP ${res.status}`);
  }
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : (res.text() as unknown)) as Promise<T>;
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Trigger a browser download for an export endpoint (keeps the session cookie). */
export async function download(path: string) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, msg);
  }
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/.exec(cd) || /filename="?([^";]+)"?/.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : "export";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function todayISO(): string {
  // Business date comes from the server; this is only a default for date pickers (local time).
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function fmtMoney(n: number | null | undefined, currency = "INR"): string {
  if (n === null || n === undefined) return "-";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency, maximumFractionDigits: 0 }).format(n);
  } catch {
    return `${currency} ${Math.round(n)}`;
  }
}

export const FLAG_LABELS: Record<string, string> = {
  NOT_IN_OMS: "Not in OMS yet",
  NOT_RTS: "Not packed in OMS",
  PARTIAL_CANCEL: "Partly cancelled",
  ALREADY_SHIPPED_IN_OMS: "Already shipped in OMS",
  STATUS_CHANGED: "OMS status changed",
  CHANNEL_UNMAPPED: "Channel unmapped",
  WRONG_CHANNEL: "Wrong marketplace",
  CANCELLED: "Cancelled",
  RETURN: "Return",
  FLAGGED: "Flagged by packer",
};
