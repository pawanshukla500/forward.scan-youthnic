import { KeyRound, X } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { api, PASSWORD_RULE, passwordProblem, type User } from "../api";
import { useAuth } from "../App";
import { Button, Field, inputCls } from "./ui";

/** Current password + new password twice. Signs out every other session of this account. */
function ChangePasswordForm({ forced, onDone, onCancel }: { forced?: boolean; onDone: () => void; onCancel?: () => void }) {
  const { setUser } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = !current ? "Enter your current password" : passwordProblem(next) ?? (next !== again ? "The two new passwords are not the same" : null);
    if (problem) {
      setErr(problem);
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ user: User }>("/api/auth/change-password", { method: "POST", json: { current_password: current, new_password: next } });
      setUser(r.user);
      onDone();
    } catch (e2) {
      setErr((e2 as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const type = show ? "text" : "password";
  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4" noValidate>
      <Field label={forced ? "Password you were given" : "Current password"}>
        <input id="cp-current" className={inputCls} type={type} autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} autoFocus />
      </Field>
      <Field label="New password" hint={PASSWORD_RULE}>
        <input id="cp-new" className={inputCls} type={type} autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      <Field label="New password again">
        <input id="cp-again" className={inputCls} type={type} autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
      </Field>
      <label className="flex cursor-pointer items-center gap-2 text-sm text-ink-2">
        <input id="cp-show" type="checkbox" className="size-4" checked={show} onChange={(e) => setShow(e.target.checked)} /> Show passwords
      </label>
      {err && (
        <p className="rounded-lg bg-crit-wash px-3 py-2 text-sm text-crit-ink" role="alert">
          {err}
        </p>
      )}
      <div className="flex justify-end gap-2 pt-1">
        {onCancel && (
          <Button type="button" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button type="submit" variant="primary" loading={busy}>
          {forced ? "Set my password" : "Change password"}
        </Button>
      </div>
    </form>
  );
}

/** Full page shown after an admin created or reset the account: nothing else opens until a new password is set. */
export function ChangePasswordPage() {
  const { user, logout } = useAuth();
  return (
    <div className="grid min-h-screen place-items-center bg-page p-4">
      <div className="card w-full max-w-md space-y-5 p-6">
        <div>
          <span className="eyebrow">ForwardScan</span>
          <h1 className="mt-1 flex items-center gap-2 text-xl font-bold">
            <KeyRound className="size-5 text-accent-ink" aria-hidden /> Choose your own password
          </h1>
          <p className="mt-1 text-sm text-muted">
            {user?.full_name || user?.username}, an admin set your current password. Pick a new one that only you know before you continue.
          </p>
        </div>
        <ChangePasswordForm forced onDone={() => {}} />
        <button type="button" className="text-sm text-muted underline-offset-2 hover:underline" onClick={() => void logout()}>
          Sign out instead
        </button>
      </div>
    </div>
  );
}

export function ChangePasswordDialog({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-[rgba(4,12,8,0.58)] p-4 backdrop-blur-sm" onMouseDown={onClose}>
      <div onMouseDown={(e) => e.stopPropagation()} className="card flash-in w-full max-w-md space-y-4 p-6" role="dialog" aria-modal="true" aria-labelledby="cp-title">
        <div className="flex items-start justify-between gap-3">
          <div>
            <span className="eyebrow">Your account</span>
            <h2 id="cp-title" className="mt-1 text-xl font-bold">
              Change password
            </h2>
            <p className="text-sm text-muted">Other devices signed in with this account are signed out.</p>
          </div>
          <button type="button" onClick={onClose} className="grid size-10 cursor-pointer place-items-center rounded-lg text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close">
            <X className="size-5" aria-hidden />
          </button>
        </div>
        <ChangePasswordForm
          onCancel={onClose}
          onDone={() => {
            onChanged();
            onClose();
          }}
        />
      </div>
    </div>
  );
}
