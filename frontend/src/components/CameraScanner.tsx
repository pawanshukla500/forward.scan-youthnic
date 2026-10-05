import { AlertTriangle, Flashlight, FlashlightOff, Settings } from "lucide-react";
import { useEffect, useRef, useState, type RefObject } from "react";
import { Capacitor, type PluginListenerHandle } from "@capacitor/core";
import { cx } from "./ui";

/* Phone-camera barcode reader that stays open: the camera runs continuously inside a window on the
   scan screen and every AWB it reads goes to onCode - no opening and closing per packet.
   Two engines, one contract:
   - installed Android app: ML Kit's embedded scanner (startScan). The camera preview is drawn by
     Android *behind* the WebView, so the camera window here is transparent (html.cam-native makes the
     page see-through; the scan screen paints everything except the window). Only barcodes whose
     position falls inside the visible window count, so labels lying elsewhere on the table are ignored;
   - browser: rear camera <video> + the browser's BarcodeDetector (Chrome on Android).
   The same barcode is ignored while it stays in view so one label is never submitted twice. */

interface Detected {
  rawValue: string;
  format?: string;
}
interface DetectorCtor {
  new (o: { formats: string[] }): { detect(src: CanvasImageSource): Promise<Detected[]> };
  getSupportedFormats?: () => Promise<string[]>;
}

/* AWB / courier labels use 1-D barcodes (usually Code 128). The square QR / DataMatrix /
   PDF417 codes printed on the same label (UPI, returns, apps) must never become scans,
   so 2-D formats are deliberately not requested - and filtered again on every read. */
const FORMATS = ["code_128", "code_39", "code_93", "codabar", "itf", "ean_13", "ean_8", "upc_a", "upc_e"];
const IGNORED_FORMATS = new Set(["qr_code", "data_matrix", "pdf417", "aztec"]);

/** Same label held in front of the camera: ignore re-reads for this long (was 4s -> duplicate spam). */
const SAME_CODE_SUPPRESS_MS = 8000;
/** Any two camera reads closer than this are the same frame seen twice - drop the second. */
const ANY_CODE_COOLDOWN_MS = 2200;
/** Native barcode positions come from the camera frame, not the page: allow this much slack (CSS px). */
const WINDOW_SLACK_PX = 40;

/** QR payloads / URLs / multi-word text can never be an AWB - drop them before they reach the server. */
export function looksLikeQr(value: string): boolean {
  const v = value.trim();
  if (!v) return true;
  if (/\s/.test(v)) return true;
  const low = v.toLowerCase();
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(low) || v.includes("://")) return true;
  if (/^(https?:|www\.|upi:|mailto:|tel:|smsto:|sms:|geo:|wifi:|begin:|mecard|vcard)/i.test(v)) return true;
  if (v.length > 60) return true;
  const norm = v.toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (norm.length < 6 || norm.length > 40) return true;
  if (/^(HTTP|WWW|UPI|VCARD|MECARD|WIFI)/.test(norm)) return true;
  return false;
}

export const isNativeApp = () => Capacitor.isNativePlatform();

export function cameraProblem(): string | null {
  if (isNativeApp()) return null; // the app uses ML Kit, not the browser camera APIs
  if (!window.isSecureContext)
    return "The camera only works on a secure address (https://, or localhost on this PC). On this network address use a hardware scanner or Manual entry.";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser cannot open the camera. Use Chrome on Android, or Manual entry.";
  if (!(window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector)
    return "This browser cannot read barcodes from the camera. Use Chrome on Android, or Manual entry.";
  return null;
}

/** Drops QR / junk, re-reads of the label still in view, and double frames. */
function useCodeGate(onCode: (value: string) => void, paused: boolean) {
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const seen = useRef({ code: "", at: 0, emittedAt: 0 });
  return useRef((value: string, format: string) => {
    const v = value.trim();
    const now = Date.now();
    const s = seen.current;
    if (!v || IGNORED_FORMATS.has(format.toLowerCase()) || looksLikeQr(v)) return;
    if (v === s.code) {
      // still the same label in view: keep it suppressed, never resubmit it
      const stale = now - s.at > SAME_CODE_SUPPRESS_MS;
      s.at = now;
      if (!stale) return;
    }
    if (pausedRef.current) return; // checking the previous packet - this one is read again next frame
    if (now - s.emittedAt < ANY_CODE_COOLDOWN_MS) return;
    s.code = v;
    s.at = now;
    s.emittedAt = now;
    onCodeRef.current(v);
  }).current;
}

export function CameraScanner({
  paused,
  checking,
  onCode,
  onClose,
  windowRef,
}: {
  paused: boolean;
  checking?: boolean;
  onCode: (value: string) => void;
  onClose: () => void;
  /** the visible camera window: native reads outside it are ignored */
  windowRef: RefObject<HTMLElement | null>;
}) {
  const nativeApp = isNativeApp();
  const video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(() => cameraProblem());
  const [settingsHint, setSettingsHint] = useState(false);
  const [live, setLive] = useState(false);
  const [torch, setTorch] = useState<{ available: boolean; on: boolean }>({ available: false, on: false });
  const toggleTorch = useRef<() => Promise<boolean>>(async () => false);
  const gate = useCodeGate(onCode, paused);

  /* Installed app: ML Kit's embedded scanner, started once and kept running. */
  useEffect(() => {
    if (!nativeApp) return;
    let cancelled = false;
    const handles: PluginListenerHandle[] = [];
    let plugin: typeof import("@capacitor-mlkit/barcode-scanning").BarcodeScanner | null = null;
    const root = document.documentElement;
    (async () => {
      try {
        const { BarcodeScanner, BarcodeFormat } = await import("@capacitor-mlkit/barcode-scanning");
        plugin = BarcodeScanner;
        const sup = await BarcodeScanner.isSupported().catch(() => ({ supported: false }));
        if (cancelled) return;
        if (!sup.supported) {
          setError("This device has no camera the scanner can use. Use Manual entry.");
          return;
        }
        // The APK declares the camera in its manifest (CI injects it), so this shows the system
        // Allow/Deny dialog on first use; after a "Don't allow" only App info can grant it.
        const perm = await BarcodeScanner.checkPermissions().catch(() => null);
        if (cancelled) return;
        if (perm?.camera !== "granted") {
          const req = await BarcodeScanner.requestPermissions().catch(() => null);
          if (cancelled) return;
          if (!req || req.camera !== "granted") {
            setSettingsHint(true);
            setError("Camera access is needed to scan. Tap Open settings → Permissions → Camera → Allow, then come back.");
            return;
          }
        }
        const onRead = await BarcodeScanner.addListener("barcodesScanned", ({ barcodes }) => {
          const box = windowRef.current?.getBoundingClientRect();
          const dpr = window.devicePixelRatio || 1;
          for (const b of barcodes) {
            const pts = b.cornerPoints;
            if (box && pts?.length) {
              // corner points are in screen pixels: keep only barcodes centred inside the window
              const x = pts.reduce((a, p) => a + p[0], 0) / pts.length / dpr;
              const y = pts.reduce((a, p) => a + p[1], 0) / pts.length / dpr;
              const inside =
                x >= box.left - WINDOW_SLACK_PX && x <= box.right + WINDOW_SLACK_PX && y >= box.top - WINDOW_SLACK_PX && y <= box.bottom + WINDOW_SLACK_PX;
              if (!inside) continue;
            }
            gate(b.displayValue || b.rawValue || "", String(b.format ?? ""));
          }
        });
        if (cancelled) {
          void onRead.remove();
          return;
        }
        handles.push(onRead);
        root.classList.add("cam-native");
        await BarcodeScanner.startScan({
          formats: [
            BarcodeFormat.Code128, BarcodeFormat.Code39, BarcodeFormat.Code93, BarcodeFormat.Codabar,
            BarcodeFormat.Itf, BarcodeFormat.Ean13, BarcodeFormat.Ean8, BarcodeFormat.UpcA, BarcodeFormat.UpcE,
          ],
        });
        if (cancelled) {
          void BarcodeScanner.stopScan().catch(() => {});
          return;
        }
        setLive(true);
        const t = await BarcodeScanner.isTorchAvailable().catch(() => ({ available: false }));
        if (!cancelled) setTorch({ available: t.available, on: false });
        toggleTorch.current = async () => {
          await BarcodeScanner.toggleTorch();
          return (await BarcodeScanner.isTorchEnabled()).enabled;
        };
      } catch (e) {
        root.classList.remove("cam-native");
        if (cancelled) return;
        const msg = (e as Error)?.message || "";
        if (/permission/i.test(msg)) {
          setSettingsHint(true);
          setError("Camera access is needed to scan. Tap Open settings → Permissions → Camera → Allow, then come back.");
        } else setError("Could not start the camera. Close other camera apps, or use Manual entry.");
      }
    })();
    return () => {
      cancelled = true;
      root.classList.remove("cam-native");
      handles.forEach((h) => void h.remove());
      void plugin?.stopScan().catch(() => {});
    };
  }, [nativeApp, gate, windowRef]);

  /* Browser: rear camera + BarcodeDetector, 5 reads a second. */
  useEffect(() => {
    if (nativeApp || cameraProblem()) return;
    let stream: MediaStream | null = null;
    let timer = 0;
    let stopped = false;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
          audio: false,
        });
        if (stopped || !video.current) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        const v = video.current;
        v.srcObject = stream;
        await v.play();
        setLive(true);
        const track = stream.getVideoTracks()[0];
        const caps = (track?.getCapabilities?.() ?? {}) as MediaTrackCapabilities & { torch?: boolean };
        if (caps.torch) {
          setTorch({ available: true, on: false });
          let on = false;
          toggleTorch.current = async () => {
            on = !on;
            await track.applyConstraints({ advanced: [{ torch: on } as unknown as MediaTrackConstraintSet] });
            return on;
          };
        }
        const Ctor = (window as unknown as { BarcodeDetector: DetectorCtor }).BarcodeDetector;
        const supported = (await Ctor.getSupportedFormats?.().catch(() => FORMATS)) ?? FORMATS;
        const detector = new Ctor({ formats: FORMATS.filter((f) => supported.includes(f)) });
        const tick = async () => {
          if (stopped) return;
          if (v.readyState >= 2) {
            try {
              const found = (await detector.detect(v))[0];
              if (found?.rawValue) gate(found.rawValue, found.format ?? "");
            } catch {
              /* a frame that cannot be decoded - keep going */
            }
          }
          timer = window.setTimeout(tick, 200);
        };
        void tick();
      } catch (e) {
        const name = (e as DOMException).name;
        setError(
          name === "NotAllowedError"
            ? "Camera permission was blocked. Allow camera access for this site in the browser settings, then try again."
            : name === "NotFoundError"
              ? "No camera was found on this device."
              : `Could not open the camera (${(e as Error).message}).`,
        );
      }
    })();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [nativeApp, gate]);

  async function flipTorch() {
    try {
      const on = await toggleTorch.current();
      setTorch((t) => ({ ...t, on }));
    } catch {
      setTorch({ available: false, on: false });
    }
  }

  if (error)
    return (
      <div className="cam-error" role="alert">
        <AlertTriangle className="size-6 shrink-0" aria-hidden />
        <p>{error}</p>
        <div className="flex flex-wrap justify-center gap-2">
          {settingsHint && nativeApp && (
            <button
              type="button"
              onClick={() => void import("@capacitor-mlkit/barcode-scanning").then(({ BarcodeScanner }) => BarcodeScanner.openSettings())}
              className="cam-error-btn primary"
            >
              <Settings className="size-4" aria-hidden /> Open settings
            </button>
          )}
          <button type="button" onClick={onClose} className="cam-error-btn">
            Use manual entry
          </button>
        </div>
      </div>
    );

  return (
    <div className={cx("cam-view", nativeApp && "native")}>
      {!nativeApp && <video ref={video} muted playsInline aria-label="Rear camera preview" />}
      <div className="cam-overlay" aria-hidden>
        <div className="cam-frame">{live && !paused && <i className="scanline" />}</div>
      </div>
      <span className="cam-hint" role="status">
        {checking ? "Checking OMSGuru… hold steady" : !live ? "Starting the camera…" : "Point at the AWB barcode (the long lines, not the square QR)"}
      </span>
      {torch.available && (
        <button type="button" className="cam-btn" onClick={() => void flipTorch()} aria-pressed={torch.on} aria-label={torch.on ? "Turn torch off" : "Turn torch on"}>
          {torch.on ? <FlashlightOff className="size-5" aria-hidden /> : <Flashlight className="size-5" aria-hidden />}
        </button>
      )}
    </div>
  );
}
