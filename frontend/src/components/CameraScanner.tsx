import { AlertTriangle, ScanLine, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";

/* Phone-camera barcode reader. Two engines, one contract (onCode per AWB, continuous):
   - installed Android app: the native ML Kit scanner (no browser needed, works from the APK);
   - browser: rear camera + the browser's BarcodeDetector (Chrome on Android).
   Both keep scanning after each read so a packer moves packet to packet; the same barcode is
   ignored while it stays in view so one label is never submitted twice. */

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

export function cameraProblem(): string | null {
  if (!window.isSecureContext)
    return "The camera only works on a secure address (https://, or localhost on this PC). On this network address use a hardware scanner or Manual entry.";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser cannot open the camera. Use Chrome on Android, or Manual entry.";
  if (!(window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector)
    return "This browser cannot read barcodes from the camera. Use Chrome on Android, or Manual entry.";
  return null;
}

export function CameraScanner({ paused, checking, onCode, onClose }: { paused: boolean; checking?: boolean; onCode: (value: string) => void; onClose: () => void }) {
  const nativeApp = Capacitor.isNativePlatform();
  const video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(() => (nativeApp ? null : cameraProblem()));
  const [runId, setRunId] = useState(0); // bump to restart the native scanner after the user backs out of it
  const [nativeState, setNativeState] = useState<"starting" | "ready" | "scanning">("starting");
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;

  /* Installed app: native ML Kit loop. Opens the system scanner, submits the AWB, and opens it
     again for the next packet - fully continuous, no taps. Backing out of the scanner leaves the
     launcher so the packer can start again with one tap. */
  useEffect(() => {
    if (!nativeApp) return;
    let cancelled = false;
    let timer = 0;
    (async () => {
      try {
        const { BarcodeScanner, BarcodeFormat } = await import("@capacitor-mlkit/barcode-scanning");
        if (cancelled) return;
        const sup = await BarcodeScanner.isSupported().catch(() => ({ supported: false }));
        if (cancelled) return;
        if (!sup.supported) {
          setError("This device cannot scan barcodes. Use Manual entry.");
          return;
        }
        const perm = await BarcodeScanner.checkPermissions().catch(() => null);
        if (!cancelled && perm?.camera !== "granted") {
          const req = await BarcodeScanner.requestPermissions().catch(() => null);
          if (!req || req.camera !== "granted") {
            setError("Camera permission was blocked. Allow camera access for Forward Scan, then try again.");
            return;
          }
        }
        if (cancelled) return;
        setNativeState("ready");
        const formats = [
          BarcodeFormat.Code128, BarcodeFormat.Code39, BarcodeFormat.Code93, BarcodeFormat.Codabar,
          BarcodeFormat.Itf, BarcodeFormat.Ean13, BarcodeFormat.Ean8, BarcodeFormat.UpcA, BarcodeFormat.UpcE,
        ];
        const loop = async (): Promise<void> => {
          if (cancelled) return;
          if (pausedRef.current) {
            timer = window.setTimeout(() => void loop(), 400);
            return;
          }
          setNativeState("scanning");
          try {
            const { barcodes } = await BarcodeScanner.scan({ formats });
            if (cancelled) return;
            const v = (barcodes[0]?.displayValue || barcodes[0]?.rawValue || "").trim();
            if (v && !looksLikeQr(v)) onCodeRef.current(v);
            setNativeState("ready");
            timer = window.setTimeout(() => void loop(), 150); // next packet
          } catch {
            // user backed out of the scanner - stay ready for one tap to start again
            if (!cancelled) setNativeState("ready");
          }
        };
        void loop();
      } catch {
        if (!cancelled) setError("Could not start the barcode scanner. Use Manual entry.");
      }
    })();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [nativeApp, runId]);

  useEffect(() => {
    if (nativeApp || cameraProblem()) return;
    let stream: MediaStream | null = null;
    let timer = 0;
    let stopped = false;
    let lastCode = "";
    let lastSeen = 0;
    let lastEmittedAt = 0;
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
        const Ctor = (window as unknown as { BarcodeDetector: DetectorCtor }).BarcodeDetector;
        const supported = (await Ctor.getSupportedFormats?.().catch(() => FORMATS)) ?? FORMATS;
        const detector = new Ctor({ formats: FORMATS.filter((f) => supported.includes(f)) });
        const tick = async () => {
          if (stopped) return;
          if (!pausedRef.current && v.readyState >= 2) {
            try {
              const found = (await detector.detect(v))[0];
              const format = (found?.format ?? "").toLowerCase();
              const value = found?.rawValue?.trim();
              const now = Date.now();
              if (value && !IGNORED_FORMATS.has(format) && !looksLikeQr(value)) {
                if (value !== lastCode || now - lastSeen > SAME_CODE_SUPPRESS_MS) {
                  if (now - lastEmittedAt > ANY_CODE_COOLDOWN_MS) {
                    lastCode = value;
                    lastEmittedAt = now;
                    onCodeRef.current(value);
                  }
                }
                lastSeen = now;
              } else if (value) {
                // QR / junk in view: do not submit, and do not let it block the real barcode.
                lastSeen = value === lastCode ? now : lastSeen;
              }
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
  }, []);

  if (error)
  if (nativeApp && !error)
    return (
      <div className="relative -mx-4 mt-4 flex h-[300px] flex-col items-center justify-center gap-3 overflow-hidden bg-[#07100c] px-6 text-center text-white sm:-mx-6">
        <div className="relative grid h-[120px] w-full max-w-[420px] place-items-center rounded-sm border-2 border-white/85 shadow-[0_0_0_999px_rgba(0,0,0,0.32)]">
          {nativeState === "scanning" && <i className="scanline absolute inset-x-[3%] h-0.5 bg-[#65e4ad] shadow-[0_0_8px_#65e4ad]" />}
        </div>
        <span className="text-sm font-medium [text-shadow:0_1px_3px_#000]">
          {checking
            ? "Checking OMSGuru… hold the packet steady"
            : nativeState === "starting"
              ? "Starting the scanner…"
              : "Point at the AWB barcode (straight lines, not the square QR)"}
        </span>
        {nativeState === "ready" && !checking && (
          <button
            type="button"
            onClick={() => setRunId((n) => n + 1)}
            className="ease-ui flex min-h-12 cursor-pointer items-center gap-2 rounded-xl bg-accent px-5 text-base font-bold text-on-accent active:opacity-80"
          >
            <ScanLine className="size-5" aria-hidden /> Scan packet
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="absolute right-3 top-3 grid size-10 cursor-pointer place-items-center rounded-full bg-black/60 text-white"
          aria-label="Close camera"
        >
          <X className="size-5" aria-hidden />
        </button>
      </div>
    );

  return (
      <div className="mt-4 flex items-start gap-2.5 rounded-lg bg-warn-wash p-3 text-sm text-warn-ink" role="alert">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span className="flex-1">{error}</span>
        <button type="button" onClick={onClose} className="min-h-9 shrink-0 cursor-pointer rounded-md border border-current px-2.5 text-xs font-bold">
          Use manual
        </button>
      </div>
    );

  return (
    <div className="relative -mx-4 mt-4 h-[300px] overflow-hidden bg-[#07100c] sm:-mx-6">
      <video ref={video} muted playsInline className="size-full object-cover" aria-label="Rear camera preview" />
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-white">
        <div className="relative grid h-[120px] w-full max-w-[420px] place-items-center rounded-sm border-2 border-white/85 shadow-[0_0_0_999px_rgba(0,0,0,0.32)]">
          {!paused && <i className="scanline absolute inset-x-[3%] h-0.5 bg-[#65e4ad] shadow-[0_0_8px_#65e4ad]" />}
        </div>
        <span className="text-sm font-medium [text-shadow:0_1px_3px_#000]">
          {checking
            ? "Checking OMSGuru… hold the packet steady"
            : paused
              ? "Paused - getting the scanner ready…"
              : "Point the rear camera at the AWB barcode (straight lines, not the square QR)"}
        </span>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="absolute right-3 top-3 grid size-10 cursor-pointer place-items-center rounded-full bg-black/60 text-white"
        aria-label="Close camera"
      >
        <X className="size-5" aria-hidden />
      </button>
    </div>
  );
}
