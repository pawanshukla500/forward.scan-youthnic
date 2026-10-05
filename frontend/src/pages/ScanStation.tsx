import {
  AlertTriangle,
  Camera,
  Download,
  Keyboard,
  Maximize2,
  Minimize2,
  ScanLine,
  Undo2,
  Volume2,
  VolumeX,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, download, qs, type Channel, type QueueRow, type Scan, type ScanContext, type ScanResponse } from "../api";
import { isSupervisor, useAuth } from "../App";
import { cameraProblem, looksLikeQr } from "../components/CameraScanner";
import { PhoneScanMode } from "../components/PhoneScanMode";
import { CODE_TITLE, fmtMins, JourneyCard, KIND_META, scanKind, ShipmentCard, type ScanKind } from "../components/ShipmentCard";
import { ResultSheet } from "../components/ResultSheet";
import { SyncBanner, SyncChip, useSyncState } from "../components/SyncNotice";
import { Button, cx, Empty, IconButton, ResultPill, Skeleton, Toast } from "../components/ui";
import { useLive, useThrottled } from "../live";
import { playCue, unlockAudio } from "../sound";

interface Prefs {
  sound: boolean;
}

function loadPrefs(): Prefs {
  try {
    return { sound: true, ...JSON.parse(localStorage.getItem("fs_prefs") || "{}") };
  } catch {
    return { sound: true };
  }
}

function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage may be blocked */
  }
}

function read(key: string): string {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

const LEGEND: ScanKind[] = ["ok", "duplicate", "notfound", "check", "stop"];

const PHONE_QUERY = "(max-width: 767px)";
const isPhone = () => window.matchMedia(PHONE_QUERY).matches;
const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

/** Phone-sized screen, kept current when the phone rotates or the window is resized. */
function usePhone(): boolean {
  const [phone, setPhone] = useState(isPhone);
  useEffect(() => {
    const mq = window.matchMedia(PHONE_QUERY);
    const on = () => setPhone(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return phone;
}

/** What each verdict colour and sound means - tap one to hear it. */
function SoundLegend() {
  return (
    <ul className="sound-legend" aria-label="What each colour and sound means - tap to hear it">
      {LEGEND.map((k) => (
        <li key={k}>
          <button
            type="button"
            onClick={() => {
              unlockAudio();
              playCue(k, 0.7);
            }}
            title="Tap to hear this sound"
          >
            <i className={`verdict-${k}`} aria-hidden />
            <b>{KIND_META[k].title}</b>
            <span>{KIND_META[k].sound}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

interface Last {
  res: ScanResponse;
  raw: string;
  at: number;
}

export default function ScanStation() {
  const { channelId } = useParams();
  const cid = Number(channelId);
  const nav = useNavigate();
  const { user } = useAuth();
  const [ctx, setCtx] = useState<ScanContext | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [recent, setRecent] = useState<Scan[]>([]);
  const [mineToday, setMineToday] = useState(0);
  const [last, setLast] = useState<Last | null>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [station, setStation] = useState(() => read("fs_station"));
  const [full, setFull] = useState(false);
  const [focused, setFocused] = useState(true);
  // phones: camera by default, unless this browser/address cannot use it (then the typed box / Bluetooth scanner)
  const [mode, setMode] = useState<"camera" | "manual">(() => (cameraProblem() ? "manual" : (read("fs_scan_mode") as "camera" | "manual") || "camera"));
  const [camOpen, setCamOpen] = useState(false);
  const [tab, setTab] = useState<"queue" | "recent">("queue");
  const [flagging, setFlagging] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [now, setNow] = useState(Date.now());
  // phone verdict sheet dismissed for this result (X / backdrop / Escape): the inline card below keeps the details
  const [sheetGone, setSheetGone] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const queue = useRef<string[]>([]);
  const running = useRef(false);
  // client-side guard against the camera firing the same label twice: normalised value + time
  const lastSubmit = useRef<{ norm: string; at: number }>({ norm: "", at: 0 });
  const camRef = useRef(false);
  camRef.current = camOpen;
  const phone = usePhone();
  // phones scanning with the camera get the dedicated screen: camera on top, result below, nothing pops up
  const camScreen = phone && mode === "camera" && camOpen;
  const sync = useSyncState();

  useEffect(() => store("fs_last_channel", String(cid)), [cid]);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(t);
  }, []);

  /* ---- data ---- */
  const lastScanAt = useRef(0); // server time of this station's newest saved scan
  const loadCtx = useCallback(() => {
    const get = (again: boolean) =>
      api<ScanContext>(`/api/scan-context${qs({ channel_id: cid, limit: 12 })}`)
        .then((c) => {
          setCtx(c);
          // the shared answer can be up to a second old: if it predates our own newest scan, ask once more
          if (again && Date.parse(c.server_time) < lastScanAt.current) window.setTimeout(() => void get(false), 1100);
        })
        .catch(() => {});
    void get(true);
  }, [cid]);
  const loadRecent = useCallback(() => {
    api<{ scans: Scan[]; mine_today: number }>(`/api/scans/recent${qs({ channel_id: cid, limit: 40 })}`).then((r) => {
      setRecent(r.scans);
      setMineToday(r.mine_today);
    });
  }, [cid]);
  const ctxSoon = useThrottled(loadCtx, 900);

  useEffect(() => {
    setCtx(null);
    setLast(null);
    loadCtx();
    loadRecent();
  }, [loadCtx, loadRecent]);
  useEffect(() => {
    api<{ channels: Channel[] }>("/api/channels").then((r) => setChannels(r.channels.filter((c) => c.scan_enabled)));
  }, []);

  // Keep the scan box focused: USB scanners type into whatever has focus. Not on a phone with the
  // camera view open (focusing would pop the keyboard over the camera view).
  useEffect(() => {
    const t = window.setInterval(() => {
      if (isPhone() && camRef.current) return;
      const a = document.activeElement as HTMLElement | null;
      const typing =
        a && (a.tagName === "INPUT" || a.tagName === "SELECT" || a.tagName === "TEXTAREA" || a.closest("[role=menu],[role=dialog]")) && a !== inputRef.current;
      if (!typing && document.visibilityState === "visible") inputRef.current?.focus();
    }, 600);
    return () => window.clearInterval(t);
  }, [mode]);

  // Phones open the rear camera straight away (as in the design) when it can work here.
  useEffect(() => {
    if (isPhone() && mode === "camera") setCamOpen(true);
    // only on first open of the page
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Continuous flow on phones: the verdict sheet dismisses itself (OK fastest, stop verdicts linger),
  // so the packer never has to tap Next/Continue. Dismissing the sheet by hand cancels the timer,
  // leaving the inline card below readable; the next scan starts a fresh timer.
  // The camera screen keeps the result in its bottom half until the next scan replaces it.
  useEffect(() => {
    if (!last || !phone || camScreen || sheetGone === last.at) return;
    const ms = { ok: 1500, duplicate: 2200, notfound: 2500, check: 3000, stop: 4500 }[scanKind(last.res)] ?? 2500;
    const t = window.setTimeout(() => setLast(null), ms);
    return () => window.clearTimeout(t);
  }, [last, sheetGone, phone, camScreen]);

  // One sound per kind of result (no voice): OK, duplicate, not found, check, stop.
  const feedback = useCallback(
    (res: ScanResponse) => {
      if (prefs.sound) playCue(scanKind(res), 0.7);
    },
    [prefs.sound],
  );

  useLive((event, data) => {
    if (event === "sync") ctxSoon();
    if (event === "scan_rejected" && data.channel_id === cid) ctxSoon();
    if (event === "scan" && data.channel_id === cid) {
      setRecent((r) => (r.some((s) => s.id === data.id) ? r : [data as Scan, ...r].slice(0, 40)));
      ctxSoon();
    } else if (event === "scan_updated" && data.channel_id === cid) {
      setRecent((r) => r.map((s) => (s.id === data.id ? (data as Scan) : s)));
      ctxSoon();
      const mine = last?.res.scan?.id === data.id;
      if (mine && data.alert && data.user_id === user?.id && !last?.res.scan?.alert) {
        const res: ScanResponse = { severity: "error", code: "ALERT", message: data.alert.replace("AFTER SCAN: ", ""), scan: data, order: data.order };
        setLast({ res, raw: data.tracking, at: Date.now() });
        feedback(res);
      } else if (mine && last && last.res.scan?.result === "UNVERIFIED" && data.result !== "UNVERIFIED") {
        // the order reached OMSGuru after the scan: show its details in place
        setLast({
          ...last,
          res: {
            ...last.res,
            severity: data.result === "OK" ? "success" : "warning",
            code: data.result === "OK" ? "OK" : (data.flags?.[0] ?? "WARN"),
            message: "Order found in OMSGuru after the scan - details below",
            scan: data,
            order: data.order,
            live: "fresh",
          },
        });
      } else if (mine && last) {
        setLast({ ...last, res: { ...last.res, scan: data } });
      }
    } else if (event === "scan_voided" && data.channel_id === cid) {
      setRecent((r) => r.filter((s) => s.id !== data.id));
      ctxSoon();
    }
  });

  /* ---- scanning ---- */
  const pump = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    while (queue.current.length) {
      const raw = queue.current.shift()!;
      let res: ScanResponse;
      try {
        res = await api<ScanResponse>("/api/scan", { method: "POST", json: { channel_id: cid, tracking: raw, station } });
      } catch (e) {
        res = { severity: "error", code: "NETWORK", message: `${(e as Error).message} - scan again` };
      }
      setLast({ res, raw, at: Date.now() });
      feedback(res);
      if (res.scan && res.severity !== "error") {
        const saved = res.scan;
        lastScanAt.current = Math.max(lastScanAt.current, Date.parse(saved.scanned_at));
        setMineToday((n) => n + 1);
        setRecent((r) => (r.some((s) => s.id === saved.id) ? r : [saved, ...r].slice(0, 40)));
        // take it off the dispatch queue straight away; the server count follows
        setCtx((c) => (c ? { ...c, queue: c.queue.filter((q) => norm(q.awb) !== norm(saved.tracking_norm) && norm(q.awb) !== norm(saved.tracking)) } : c));
        ctxSoon();
      }
      // Continuous flow: the scanner never pauses. Every verdict (including wrong marketplace,
      // cancelled, duplicates) pops the sheet with its sound and the line keeps moving.
    }
    running.current = false;
    setBusy(false);
  }, [cid, station, feedback, ctxSoon]);

  const submitRaw = useCallback(
    (raw: string, fromCamera = false) => {
      unlockAudio();
      const v = raw.trim();
      if (!v) return;
      // The camera must never submit QR payloads (URLs / QR text on the label): ignore silently so the
      // packer is not spammed with errors while aiming at the AWB barcode. Typed / hardware-scanner input
      // still goes to the server, which answers INVALID with guidance.
      if (fromCamera && looksLikeQr(v)) return;
      const n = norm(v);
      const now = Date.now();
      // Same label re-read within 8s (label still in view) or already waiting in the queue: drop it.
      if (n && n === lastSubmit.current.norm && now - lastSubmit.current.at < 8000) return;
      if (n && queue.current.some((q) => norm(q) === n)) return;
      lastSubmit.current = { norm: n, at: now };
      queue.current.push(v);
      void pump();
    },
    [pump],
  );

  const submitCameraCode = useCallback((code: string) => submitRaw(code, true), [submitRaw]);

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    unlockAudio();
    if (e.key !== "Enter" && e.key !== "Tab") return;
    e.preventDefault();
    const raw = value;
    setValue("");
    submitRaw(raw);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const raw = value;
    setValue("");
    submitRaw(raw);
    inputRef.current?.focus();
  }

  async function undo(s: Scan) {
    if (!window.confirm(`Remove scan ${s.tracking}? It can then be scanned again.`)) return;
    try {
      await api(`/api/scans/${s.id}`, { method: "DELETE" });
      setRecent((r) => r.filter((x) => x.id !== s.id));
      setMineToday((n) => Math.max(0, n - 1));
      if (last?.res.scan?.id === s.id) setLast(null);
      setToast({ kind: "ok", text: `Scan ${s.tracking} removed - it can be scanned again` });
      loadCtx();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  }

  async function flag(reason: string) {
    const s = last?.res.scan;
    if (!s) return;
    let note = "";
    if (reason === "Other") {
      note = (window.prompt("What is the problem with this shipment?") || "").trim();
      if (!note) return;
    }
    setFlagging(true);
    try {
      const r = await api<{ scan: Scan }>(`/api/scans/${s.id}/flag`, { method: "POST", json: { reason, note } });
      setLast((l) =>
        l && l.res.scan?.id === s.id ? { ...l, res: { ...l.res, severity: "warning", code: "FLAGGED", message: r.scan.message, scan: r.scan } } : l,
      );
      setRecent((list) => list.map((x) => (x.id === r.scan.id ? r.scan : x)));
      setToast({ kind: "ok", text: `${s.tracking} flagged for review: ${reason}` });
      loadCtx();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    } finally {
      setFlagging(false);
    }
  }

  function next() {
    setLast(null);
    if (!(isPhone() && camOpen)) inputRef.current?.focus();
  }

  function updatePrefs(p: Partial<Prefs>) {
    const n = { ...prefs, ...p };
    setPrefs(n);
    store("fs_prefs", JSON.stringify(n));
  }

  function pickMode(m: "camera" | "manual") {
    // leaving the camera screen: its result was already on screen - do not pop it up again as a sheet
    if (m === "manual" && last) setSheetGone(last.at);
    setMode(m);
    store("fs_scan_mode", m);
    setCamOpen(m === "camera");
    if (m === "manual") window.setTimeout(() => inputRef.current?.focus(), 50);
  }

  function toggleFull() {
    if (!document.fullscreenElement) void document.documentElement.requestFullscreen?.().then(() => setFull(true));
    else void document.exitFullscreen?.().then(() => setFull(false));
  }

  async function exportPending(bucket: "pending" | "overdue") {
    try {
      await download(`/api/reconciliation/export.xlsx${qs({ bucket, channel_id: cid })}`);
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  }

  /* ---- derived ---- */
  const channel = ctx?.channel ?? channels.find((c) => c.id === cid) ?? null;
  const st = ctx?.stats;
  const awb = ctx?.awb;
  const okPct = st && st.scanned ? Math.round((100 * st.ok) / st.scanned) : null;
  const base = awb ? awb.generated - awb.cancelled : 0;
  const pct = awb?.pct ?? null;
  const sev = last?.res.severity;
  const canUndoLast =
    !!last?.res.scan &&
    (isSupervisor(user) || (last.res.scan.user_id === user?.id && Date.now() - new Date(last.res.scan.scanned_at).getTime() < 10 * 60 * 1000));

  const toastEl = toast && (
    <Toast kind={toast.kind} onClose={() => setToast(null)}>
      {toast.text}
    </Toast>
  );

  const statusPill = focused || camOpen ? (
    <span className="inline-flex min-h-9 items-center gap-2 whitespace-nowrap rounded-full bg-accent-wash px-3 text-sm font-semibold text-accent-ink">
      <span className="pulse-dot size-2 rounded-full bg-good" aria-hidden />
      {busy ? (
        <>
          <span className="sm:hidden">Checking...</span>
          <span className="hidden sm:inline">Checking OMSGuru...</span>
        </>
      ) : (
        <>
          <span className="sm:hidden">Ready</span>
          <span className="hidden sm:inline">Scanner ready</span>
        </>
      )}
    </span>
  ) : (
    <button
      type="button"
      onClick={() => inputRef.current?.focus()}
      className="inline-flex min-h-9 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-full bg-warn-wash px-3 text-sm font-semibold text-warn-ink"
    >
      <AlertTriangle className="size-4" aria-hidden /> <span className="sm:hidden">Tap to scan</span>
      <span className="hidden sm:inline">Not listening - tap to reconnect</span>
    </button>
  );

  return (
    <div className="scan-layout">
      {/* only "scans are not being saved" problems get a banner here; "OMSGuru busy" & co. are a chip in the toolbar */}
      {sync?.tone === "crit" && <SyncBanner state={sync} />}

      {/* ---- shipment lookup: marketplace + scanner state, the scan box, today's numbers - one compact panel ---- */}
      <section className="scan-panel" data-verdict={last ? scanKind(last.res) : undefined} aria-labelledby="lookup-title">
        <h2 id="lookup-title" className="sr-only">
          Scan tracking ID - {channel?.name ?? ""}
        </h2>
        {/* wide screens: marketplace | scan box | scanner state on one row; narrower: toolbar above the box */}
        <div className="scan-head">
          <div className="scan-toolbar">
            <label className="strip-select">
              <i aria-hidden style={{ background: channel?.color || "var(--muted)" }} />
              <select value={cid} onChange={(e) => nav(`/scan/${e.target.value}`)} aria-label="Marketplace">
                {channels.length === 0 && channel && <option value={channel.id}>{channel.name}</option>}
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <p className="scan-hint">Scanner or keyboard · every scan is checked live with OMSGuru</p>
            <div className="scanner-controls">
              <button type="button" onClick={() => updatePrefs({ sound: !prefs.sound })} aria-pressed={prefs.sound} className={cx("sound-toggle", prefs.sound && "active")}>
                {prefs.sound ? <Volume2 className="size-4" aria-hidden /> : <VolumeX className="size-4" aria-hidden />}
                Sound {prefs.sound ? "on" : "off"}
              </button>
              {sync && sync.tone !== "crit" && <SyncChip state={sync} />}
              {statusPill}
              <span className="hidden md:inline-flex">
                <IconButton label={full ? "Exit full screen" : "Full screen"} onClick={toggleFull}>
                  {full ? <Minimize2 className="size-4" aria-hidden /> : <Maximize2 className="size-4" aria-hidden />}
                </IconButton>
              </span>
            </div>
          </div>

          {/* phones: progress strip + Camera / Manual */}
          <div className="-mx-4 mt-3 grid grid-cols-[auto_auto_1fr] items-center gap-2 border-y border-line px-4 py-2.5 text-sm text-muted md:hidden">
            <span>Scanned</span>
            <b className="tnum text-ink">
              {awb ? `${awb.scanned} / ${base}` : "-"}
            </b>
            <span className="h-1.5 overflow-hidden rounded-full bg-line" aria-hidden>
              <span className="block h-full rounded-full bg-accent" style={{ width: `${pct ?? 0}%` }} />
            </span>
          </div>
          <div className="-mx-4 grid grid-cols-2 border-b border-line md:hidden" role="tablist" aria-label="Scan method">
            {(["camera", "manual"] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={mode === m}
                onClick={() => pickMode(m)}
                className={cx(
                  "flex h-12 cursor-pointer items-center justify-center gap-2 border-b-2 text-sm font-semibold",
                  mode === m ? "border-accent text-accent-ink" : "border-transparent text-muted",
                )}
              >
                {m === "camera" ? <Camera className="size-[18px]" aria-hidden /> : <Keyboard className="size-[18px]" aria-hidden />}
                {m === "camera" ? "Camera" : "Manual"}
              </button>
            ))}
          </div>
          {mode === "camera" && !camOpen && (
            <button
              type="button"
              onClick={() => setCamOpen(true)}
              className="mt-4 flex h-12 w-full cursor-pointer items-center justify-center gap-2 rounded-lg border border-accent/40 bg-accent-wash text-sm font-bold text-accent-ink md:hidden"
            >
              <Camera className="size-[18px]" aria-hidden /> Open rear camera
            </button>
          )}

          <form onSubmit={onSubmit} className={cx("scan-form", mode === "camera" && camOpen && "camera-mode")}>
            <ScanLine className="size-6 shrink-0" aria-hidden />
            <input
              id="scanbox"
              ref={inputRef}
              autoFocus={!isPhone()}
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="characters"
              spellCheck={false}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={onKeyDown}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              aria-label="Tracking ID"
              aria-describedby="scan-status"
              placeholder="Scan tracking ID and press Enter"
            />
            <span>Auto opens on Enter</span>
          </form>
        </div>

        {/* one atomic announcement per scan for screen readers */}
        <div id="scan-status" className="sr-only-live" role="status" aria-live={sev === "error" ? "assertive" : "polite"} aria-atomic="true">
          {last ? `${CODE_TITLE[last.res.code] ?? last.res.code}. ${last.res.message}. ${last.res.scan?.tracking ?? last.raw}` : ""}
        </div>

        {/* today in this marketplace: scans, then AWB progress - one slim row on wide screens */}
        <div className="scan-metrics" aria-label="Today in this marketplace">
          <Metric label="Today's scans" value={st?.scanned} sub={st ? `${mineToday.toLocaleString("en-IN")} by you` : ""} subClass="positive" />
          <Metric label="Successful" value={st?.ok} sub={okPct === null || okPct === undefined ? "" : `${okPct}%`} />
          <Metric
            label="Flagged"
            value={st?.flagged}
            sub={st ? (st.flagged ? "Needs review" : "All clear") : ""}
            subClass={st?.flagged ? "warning-text" : undefined}
          />
          <div className="metric-progress">
            {!awb ? (
              <Skeleton className="h-9 w-full" />
            ) : (
              <>
                <span>
                  <b>{awb.scanned.toLocaleString("en-IN")}</b> of {awb.generated.toLocaleString("en-IN")} AWBs scanned today
                  {pct !== null && <em>{pct}%</em>}
                </span>
                <div role="progressbar" aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100} aria-label="Today's AWBs scanned">
                  <i style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
                </div>
              </>
            )}
          </div>
          <Metric label="Pending" value={awb?.pending} className="pending" />
          <Metric label="Overdue" value={awb?.overdue} className={awb && awb.overdue > 0 ? "overdue" : undefined} />
          <button type="button" className="secondary metric-download" onClick={() => void exportPending("pending")} title="Download pending AWBs (Excel)">
            <Download className="size-4" aria-hidden /> Download
          </button>
        </div>
      </section>

      {/* ---- the scanned shipment ---- */}
      {last ? (
        <div className="shipment-grid">
          <ShipmentCard
            res={last.res}
            raw={last.raw}
            at={last.at}
            canUndo={canUndoLast}
            flagging={flagging}
            onFlag={(r) => void flag(r)}
            onUndo={() => last.res.scan && void undo(last.res.scan)}
            onNext={next}
            now={now}
          />
          <JourneyCard res={last.res} ctx={ctx} now={now} />
        </div>
      ) : (
        <section className="card scan-empty">
          <span className="scan-empty-icon">
            <ScanLine className="size-6" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="text-base font-bold">{st?.scanned ? "Ready for the next shipment" : "Scan the first packet"}</p>
            <p className="mt-0.5 text-sm text-ink-2">
              Scan the AWB barcode on the shipping label. Order details and items appear here; duplicates, wrong marketplace and cancelled orders are
              stopped.
            </p>
          </div>
          <SoundLegend />
        </section>
      )}

      {/* ---- dispatch queue / recent ---- */}
      <section className="card pending-card" aria-label="Dispatch queue and recent scans">
        <div className="card-head">
          <div>
            <span className="eyebrow">Dispatch queue</span>
            <h2>{tab === "queue" ? "Pending priority scans" : "Recent scans in this marketplace"}</h2>
            <p>{tab === "queue" ? "Orders sorted by carrier cutoff and priority." : "All stations, today. Updates live."}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {tab === "queue" && (awb?.overdue ?? 0) > 0 && (
              <Button variant="ghost" onClick={() => void exportPending("overdue")}>
                <Download className="size-4" aria-hidden /> Overdue
              </Button>
            )}
            {tab === "queue" && (
              <Button onClick={() => void exportPending("pending")}>
                <Download className="size-4" aria-hidden /> Export queue
              </Button>
            )}
          </div>
        </div>
        <div className="mt-4 flex gap-1 border-b border-line px-4 sm:px-6" role="tablist" aria-label="List">
          {(
            [
              ["queue", `Pending (${(ctx?.queue_total ?? 0).toLocaleString("en-IN")})`],
              ["recent", `Recent scans (${recent.length})`],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={cx(
                "-mb-px min-h-11 cursor-pointer border-b-2 px-3 text-sm font-semibold",
                tab === k ? "border-accent text-accent-ink" : "border-transparent text-muted hover:text-ink",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === "queue" ? (
          <QueueList rows={ctx?.queue ?? null} total={ctx?.queue_total ?? 0} now={now} cid={cid} />
        ) : (
          <ul className="max-h-[520px] divide-y divide-line overflow-y-auto">
            {recent.length === 0 && <Empty title="No scans yet today" icon={ScanLine} />}
            {recent.map((s) => {
              const canUndo = isSupervisor(user) || (s.user_id === user?.id && Date.now() - new Date(s.scanned_at).getTime() < 10 * 60 * 1000);
              return (
                <li key={s.id} className="flex items-start gap-3 px-4 py-3 sm:px-6">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate font-mono text-sm font-semibold">{s.tracking}</span>
                      <ResultPill result={s.result} alert={s.alert} />
                    </div>
                    <div className="mt-0.5 truncate text-xs text-muted">
                      {s.scanned_at_local} · {s.user}
                      {s.order ? ` · ${s.order.courier || "-"} · ${s.order.channel_order_id}` : ""}
                    </div>
                    {(s.alert || s.flags.includes("FLAGGED")) && <div className="mt-1 text-xs font-medium text-crit-ink">{s.alert || s.message}</div>}
                  </div>
                  {canUndo && (
                    <IconButton label={`Remove scan ${s.tracking}`} onClick={() => void undo(s)}>
                      <Undo2 className="size-4" aria-hidden />
                    </IconButton>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <div className="scan-foot flex flex-wrap items-center gap-3 text-sm text-muted">
        <label className="inline-flex items-center gap-2">
          Station
          <input
            className="h-9 w-40 rounded-lg border border-line-strong bg-surface px-2.5 text-sm text-ink"
            placeholder="e.g. Table 3"
            value={station}
            onChange={(e) => {
              setStation(e.target.value);
              store("fs_station", e.target.value);
            }}
          />
        </label>
        <Link to="/scan" className="inline-flex min-h-9 items-center font-semibold text-accent-ink hover:underline">
          All marketplaces
        </Link>
      </div>

      {toast && !camScreen && toastEl}

      {/* phones with the camera: camera stays open in the top half, the result fills the bottom half */}
      {camScreen && (
        <PhoneScanMode
          channelName={channel?.name ?? "..."}
          channelColor={channel?.color}
          progress={awb ? { scanned: awb.scanned, total: base, pct: pct ?? 0 } : null}
          notice={sync?.tone === "crit" ? <SyncBanner state={sync} /> : undefined}
          busy={busy}
          onCode={submitCameraCode}
          onManual={() => pickMode("manual")}
          footer={toast ? toastEl : undefined}
        >
          {last ? (
            <ShipmentCard
              res={last.res}
              raw={last.raw}
              at={last.at}
              canUndo={canUndoLast}
              flagging={flagging}
              onFlag={(r) => void flag(r)}
              onUndo={() => last.res.scan && void undo(last.res.scan)}
              now={now}
            />
          ) : (
            <div className="scan-mode-idle">
              <p className="text-base font-bold">{st?.scanned ? "Ready for the next packet" : "Scan the first packet"}</p>
              <p className="mt-0.5 text-sm text-ink-2">Hold the AWB barcode inside the frame - it scans by itself and the result shows here.</p>
              <dl className="scan-mode-stats">
                <div>
                  <dt>Today</dt>
                  <dd>{st ? st.scanned.toLocaleString("en-IN") : "—"}</dd>
                </div>
                <div>
                  <dt>By you</dt>
                  <dd className="positive">{mineToday.toLocaleString("en-IN")}</dd>
                </div>
                <div>
                  <dt>Pending</dt>
                  <dd className="warning-text">{awb ? awb.pending.toLocaleString("en-IN") : "—"}</dd>
                </div>
                <div>
                  <dt>Overdue</dt>
                  <dd className={awb && awb.overdue > 0 ? "text-crit-ink" : undefined}>{awb ? awb.overdue.toLocaleString("en-IN") : "—"}</dd>
                </div>
              </dl>
              <SoundLegend />
            </div>
          )}
        </PhoneScanMode>
      )}

      {/* phones typing / with a Bluetooth scanner: immediate verdict pop-up - the next scan replaces it,
          X / backdrop / Escape dismisses it while the inline card below keeps the details.
          Continuous flow: the sheet auto-dismisses so nobody has to tap anything. */}
      {last && phone && !camScreen && sheetGone !== last.at && (
        <ResultSheet
          last={last}
          channelName={channel?.name ?? "..."}
          canUndo={canUndoLast}
          onPrimary={next}
          onDismiss={() => setSheetGone(last.at)}
          onUndo={() => last.res.scan && void undo(last.res.scan)}
        />
      )}
    </div>
  );
}

/** One number in the scan panel's metrics row: small label above, value with an optional note beside it. */
function Metric({ label, value, sub, subClass, className }: { label: string; value: number | undefined; sub?: string; subClass?: string; className?: string }) {
  return (
    <div className={cx("metric-cell", className)}>
      <span>{label}</span>
      <b>{value === undefined ? "—" : value.toLocaleString("en-IN")}</b>
      {sub && <small className={subClass}>{sub}</small>}
    </div>
  );
}

function dueText(r: QueueRow, now: number): { text: string; late: boolean } {
  if (r.sla_date) {
    const m = (new Date(r.sla_date).getTime() - now) / 60000;
    return m >= 0 ? { text: `${fmtMins(m)} to SLA`, late: false } : { text: `SLA passed ${fmtMins(m)} ago`, late: true };
  }
  if (r.age_days > 0) return { text: `AWB ${r.age_days} day${r.age_days > 1 ? "s" : ""} old`, late: true };
  return { text: "Due today", late: false };
}

function QueueList({ rows, total, now, cid }: { rows: QueueRow[] | null; total: number; now: number; cid: number }) {
  if (!rows)
    return (
      <div className="space-y-2 p-4 sm:p-6">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  if (rows.length === 0) return <Empty title="Nothing pending - every AWB generated today is scanned" icon={ScanLine} />;
  return (
    <>
      <ul className="pending-list">
        {rows.map((r) => {
          const due = dueText(r, now);
          return (
            <li key={r.awb} className="pending-row">
              <b>{r.awb}</b>
              <span>{r.order_id}</span>
              <span>
                {r.skus} SKU{r.skus !== 1 ? "s" : ""} · {r.units} unit{r.units !== 1 ? "s" : ""}
              </span>
              <span>{r.courier || "-"}</span>
              <span>{due.text}</span>
              <i className={`priority-${r.priority.toLowerCase()}`}>{r.priority}</i>
            </li>
          );
        })}
      </ul>
      {total > rows.length && (
        <div className="border-t border-line px-4 py-3 text-sm sm:px-6">
          <Link to={`/pending?channel_id=${cid}`} className="font-semibold text-accent-ink hover:underline">
            See all {total.toLocaleString("en-IN")} pending AWBs
          </Link>
        </div>
      )}
    </>
  );
}
