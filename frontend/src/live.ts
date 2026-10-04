import { useCallback, useEffect, useRef, useState } from "react";

type Handler = (event: string, data: any) => void; // eslint-disable-line @typescript-eslint/no-explicit-any

const handlers = new Set<Handler>();
const statusListeners = new Set<(up: boolean) => void>();
let socket: WebSocket | null = null;
let retry = 0;
let started = false;
let connected = false;
let pingTimer: number | undefined;

function setConnected(v: boolean) {
  connected = v;
  statusListeners.forEach((l) => l(v));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.onopen = () => {
    retry = 0;
    setConnected(true);
    window.clearInterval(pingTimer);
    pingTimer = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 25000);
  };
  socket.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.event && msg.event !== "pong") handlers.forEach((h) => h(msg.event, msg.data));
    } catch {
      /* ignore */
    }
  };
  socket.onclose = (ev) => {
    setConnected(false);
    window.clearInterval(pingTimer);
    if (ev.code === 4401 || !started) return; // signed out
    retry = Math.min(retry + 1, 6);
    window.setTimeout(connect, 500 * 2 ** retry);
  };
}

export function startLive() {
  if (started) return;
  started = true;
  connect();
}

export function stopLive() {
  started = false;
  socket?.close();
  socket = null;
}

/** Subscribe to server push events (scan, scan_rejected, scan_updated, scan_voided, sync, manifest). */
export function useLive(handler: Handler) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    const h: Handler = (e, d) => ref.current(e, d);
    handlers.add(h);
    return () => {
      handlers.delete(h);
    };
  }, []);
}

export function useLiveStatus(): boolean {
  const [up, setUp] = useState(connected);
  useEffect(() => {
    statusListeners.add(setUp);
    return () => {
      statusListeners.delete(setUp);
    };
  }, []);
  return up;
}

/** Run fn once, `wait` ms after the first of a burst of events; events in between join that run.
 *  (A trailing debounce restarted by every event never fired while a shift was scanning: in the 3 Oct 2026 load
 *  test dashboards refreshed 6 times for 1,221 scan events.) */
export function useThrottled(fn: () => void, wait: number): () => void {
  const timer = useRef<number>();
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return useCallback(() => {
    if (timer.current !== undefined) return;
    timer.current = window.setTimeout(() => {
      timer.current = undefined;
      latest.current();
    }, wait);
  }, [wait]);
}
