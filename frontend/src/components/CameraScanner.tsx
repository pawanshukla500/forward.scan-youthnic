import { AlertTriangle, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

/* Phone-camera barcode reader (rear camera + the browser's BarcodeDetector, i.e. Chrome on Android).
   It keeps scanning after each read so a packer can move from packet to packet; the same barcode is
   ignored while it stays in view so one label is never submitted twice. */

interface Detected {
  rawValue: string;
}
interface DetectorCtor {
  new (o: { formats: string[] }): { detect(src: CanvasImageSource): Promise<Detected[]> };
  getSupportedFormats?: () => Promise<string[]>;
}

const FORMATS = ["code_128", "code_39", "code_93", "codabar", "itf", "ean_13", "ean_8", "upc_a", "upc_e", "qr_code", "data_matrix", "pdf417"];

export function cameraProblem(): string | null {
  if (!window.isSecureContext)
    return "The camera only works on a secure address (https://, or localhost on this PC). On this network address use a hardware scanner or Manual entry.";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser cannot open the camera. Use Chrome on Android, or Manual entry.";
  if (!(window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector)
    return "This browser cannot read barcodes from the camera. Use Chrome on Android, or Manual entry.";
  return null;
}

export function CameraScanner({ paused, onCode, onClose }: { paused: boolean; onCode: (value: string) => void; onClose: () => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(cameraProblem);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;

  useEffect(() => {
    if (cameraProblem()) return;
    let stream: MediaStream | null = null;
    let timer = 0;
    let stopped = false;
    let lastCode = "";
    let lastSeen = 0;
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
              const value = (await detector.detect(v))[0]?.rawValue?.trim();
              const now = Date.now();
              if (value) {
                if (value !== lastCode || now - lastSeen > 4000) {
                  lastCode = value;
                  onCodeRef.current(value);
                }
                lastSeen = now;
              }
            } catch {
              /* a frame that cannot be decoded - keep going */
            }
          }
          timer = window.setTimeout(tick, 140);
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
    <div className="relative -mx-4 mt-4 h-[270px] overflow-hidden bg-[#07100c] sm:-mx-6">
      <video ref={video} muted playsInline className="size-full object-cover" aria-label="Rear camera preview" />
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 text-white">
        <div className="relative grid h-[110px] w-[78%] place-items-center rounded-sm border-2 border-white/85 shadow-[0_0_0_999px_rgba(0,0,0,0.32)]">
          {!paused && <i className="scanline absolute inset-x-[3%] h-0.5 bg-[#65e4ad] shadow-[0_0_8px_#65e4ad]" />}
        </div>
        <span className="text-sm font-medium [text-shadow:0_1px_3px_#000]">
          {paused ? "Paused - put the packet aside, then press Continue" : "Point the rear camera at the AWB barcode"}
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
