import { Camera, Keyboard, Maximize2, Minimize2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { unlockAudio } from "../sound";
import { CameraScanner } from "./CameraScanner";
import { cx } from "./ui";

/* Phone scanning screen: the camera stays open in the top half and every result lands in the bottom
   half - no camera opening and closing per packet, no pop-up over the camera. Full screen on purpose
   (fixed, above the app) so the installed app can show its native camera through the window. */

export function PhoneScanMode({
  channelName,
  channelColor,
  progress,
  notice,
  busy,
  onCode,
  onManual,
  children,
  footer,
}: {
  channelName: string;
  channelColor?: string;
  progress: { scanned: number; total: number; pct: number } | null;
  /** critical sync / connection banner (scans not being saved) */
  notice?: ReactNode;
  busy: boolean;
  onCode: (value: string) => void;
  onManual: () => void;
  /** the last result, or what to do before the first scan */
  children: ReactNode;
  footer?: ReactNode;
}) {
  const windowRef = useRef<HTMLDivElement>(null);
  const [big, setBig] = useState(false);

  // the page behind stays still while scanning
  useEffect(() => {
    const root = document.documentElement;
    root.classList.add("scan-mode-on");
    return () => root.classList.remove("scan-mode-on");
  }, []);

  return (
    <div className="scan-mode" role="dialog" aria-modal="true" aria-label={`Camera scanning - ${channelName}`} onPointerDown={unlockAudio}>
      <div className="scan-mode-top">
        <div className="scan-mode-progress">
          <i aria-hidden style={{ background: channelColor || "var(--muted)" }} />
          <b className="truncate">{channelName}</b>
          <span className="tnum">
            Scanned <b>{progress ? `${progress.scanned.toLocaleString("en-IN")} / ${progress.total.toLocaleString("en-IN")}` : "-"}</b>
          </span>
          <span className="scan-mode-bar" aria-hidden>
            <span style={{ width: `${Math.min(100, progress?.pct ?? 0)}%` }} />
          </span>
        </div>
        <div className="scan-mode-tabs" role="tablist" aria-label="Scan method">
          <button type="button" role="tab" aria-selected="true" className="active">
            <Camera className="size-[18px]" aria-hidden /> Camera
          </button>
          <button type="button" role="tab" aria-selected="false" onClick={onManual}>
            <Keyboard className="size-[18px]" aria-hidden /> Manual
          </button>
        </div>
      </div>
      {notice}
      <div ref={windowRef} className={cx("scan-mode-window", big && "big")}>
        <CameraScanner paused={busy} checking={busy} onCode={onCode} onClose={onManual} windowRef={windowRef} />
        <button type="button" className="cam-btn size" onClick={() => setBig((b) => !b)} aria-label={big ? "Smaller camera" : "Bigger camera"}>
          {big ? <Minimize2 className="size-5" aria-hidden /> : <Maximize2 className="size-5" aria-hidden />}
        </button>
      </div>
      <div className="scan-mode-result" aria-live="polite">
        {children}
      </div>
      {footer}
    </div>
  );
}
