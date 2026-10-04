// Distinct audio cues so packers know the result without looking at the screen (no voice).
//   ok        : one short high beep              - verified, put it in the bag
//   duplicate : three quick high beeps           - already scanned, set aside
//   notfound  : two tones, high then low         - not in OMSGuru yet, saved as unverified
//   check     : two medium beeps                 - saved, but look at the packet
//   stop      : long low buzzer                  - wrong marketplace / cancelled / invalid, not saved

let ctx: AudioContext | null = null;

function audio(): AudioContext | null {
  try {
    if (!ctx) ctx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

/** Browsers only allow audio after a user gesture; call this from the first click/keydown. */
export function unlockAudio() {
  audio();
}

function tone(freq: number, start: number, dur: number, type: OscillatorType, gain: number) {
  const ac = audio();
  if (!ac) return;
  const t0 = ac.currentTime + start;
  const osc = ac.createOscillator();
  const g = ac.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, t0);
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(gain, t0 + 0.01);
  g.gain.setValueAtTime(gain, t0 + dur - 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(g).connect(ac.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

export type Cue = "ok" | "duplicate" | "notfound" | "check" | "stop";

export function playCue(cue: Cue, volume = 0.6) {
  const v = Math.max(0.05, Math.min(1, volume));
  if (cue === "ok") {
    tone(1568, 0, 0.09, "square", 0.18 * v);
  } else if (cue === "duplicate") {
    for (let i = 0; i < 3; i++) tone(1319, i * 0.13, 0.07, "square", 0.22 * v);
  } else if (cue === "notfound") {
    tone(988, 0, 0.2, "triangle", 0.5 * v);
    tone(494, 0.24, 0.34, "triangle", 0.5 * v);
  } else if (cue === "check") {
    tone(880, 0, 0.13, "square", 0.22 * v);
    tone(880, 0.2, 0.13, "square", 0.22 * v);
  } else {
    tone(196, 0, 0.32, "sawtooth", 0.45 * v);
    tone(196, 0.42, 0.32, "sawtooth", 0.45 * v);
    tone(147, 0.84, 0.5, "sawtooth", 0.45 * v);
    if ("vibrate" in navigator) navigator.vibrate?.([200, 100, 200, 100, 400]);
  }
}
