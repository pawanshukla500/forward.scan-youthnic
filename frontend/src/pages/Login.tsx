import { type FormEvent, useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api, type User } from "../api";
import { useAuth } from "../App";
import { Icon, useResolvedDark } from "../components/icons";
import { cx } from "../components/ui";
import { setTheme } from "../theme";
import { unlockAudio } from "../sound";

export default function Login() {
  const { user, setUser } = useAuth();
  const nav = useNavigate();
  const dark = useResolvedDark();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [remember, setRemember] = useState(true);
  const [err, setErr] = useState("");
  const [help, setHelp] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.title = "Sign in · ForwardScan";
  }, []);

  if (user) return <Navigate to="/scan" replace />;

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    unlockAudio();
    if (!username.trim() || !password) {
      setErr("Enter your email or username and your password to continue.");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ user: User }>("/api/auth/login", { method: "POST", json: { username: username.trim(), password, remember } });
      setUser(r.user);
      nav("/scan", { replace: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cx("login-page", dark && "dark")}>
      <button className="login-theme icon-button" type="button" aria-label="Toggle color mode" onClick={() => setTheme(dark ? "light" : "dark")}>
        <Icon name={dark ? "sun" : "moon"} />
      </button>
      <aside className="login-visual" aria-label="About ForwardScan">
        <div className="login-brand">
          <div className="brand-mark">
            <Icon name="scan" size={22} />
          </div>
          <span>
            Forward<span>Scan</span>
          </span>
        </div>
        <div className="login-message">
          <span className="login-kicker">
            <i />
            Live fulfilment operations
          </span>
          <h1>
            Every shipment.
            <br />
            Scanned with confidence.
          </h1>
          <p>One workspace to scan, monitor and dispatch orders across all your marketplaces.</p>
        </div>
        <div className="login-preview">
          <div className="preview-top">
            <span>
              <i />
              Scanner ready
            </span>
            <small>Today · Dispatch warehouse</small>
          </div>
          <div className="preview-scan">
            <div>
              <Icon name="scan" size={21} />
              <span>Scan an AWB</span>
            </div>
            <b>
              <Icon name="check" size={14} />
              Ready
            </b>
          </div>
          <div className="preview-stats">
            <div>
              <span>Scanned today</span>
              <b>—</b>
            </div>
            <div>
              <span>Success rate</span>
              <b>—</b>
            </div>
            <div>
              <span>Pending</span>
              <b>—</b>
            </div>
          </div>
        </div>
        <div className="login-proof">
          <div className="proof-avatars">
            <span>FS</span>
            <span>OP</span>
            <span>WH</span>
            <span>+</span>
          </div>
          <p>Dispatch scanning for OMSGuru sales channels</p>
        </div>
      </aside>
      <main className="login-main">
        <div className="login-form-wrap">
          <div className="mobile-login-brand">
            <div className="brand-mark">
              <Icon name="scan" size={20} />
            </div>
            <span>
              Forward<span>Scan</span>
            </span>
          </div>
          <span className="eyebrow">Operations workspace</span>
          <h2>Welcome back</h2>
          <p className="login-subtitle">Sign in to continue to your scanning dashboard.</p>
          <form className="login-form" onSubmit={(e) => void submit(e)} noValidate>
            <label>
              Email or username
              <input
                autoFocus
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="name@vbexports.co.in or your login id"
                aria-invalid={!!err || undefined}
              />
            </label>
            <label>
              Password
              <div className="password-field">
                <input
                  type={show ? "text" : "password"}
                  name="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter your password"
                  aria-invalid={!!err || undefined}
                />
                <button type="button" onClick={() => setShow((v) => !v)} aria-pressed={show}>
                  {show ? "Hide" : "Show"}
                </button>
              </div>
            </label>
            {err && (
              <div className="login-error" role="alert">
                <Icon name="alert" size={15} />
                {err}
              </div>
            )}
            <div className="login-options">
              <label>
                <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                <span>
                  <Icon name="check" size={12} />
                </span>
                Keep me signed in
              </label>
              <button type="button" onClick={() => setHelp((v) => !v)} aria-expanded={help}>
                Forgot password?
              </button>
            </div>
            {help && (
              <div className="login-error" role="note">
                <Icon name="alert" size={15} />
                Ask an admin to reset it (Admin, Team members). You then choose your own password when you sign in.
              </div>
            )}
            <button className="login-submit" type="submit" disabled={busy}>
              {busy ? "Signing in…" : "Sign in to ForwardScan"} <Icon name="arrow" size={17} />
            </button>
          </form>
          <div className="login-divider">
            <span>or continue with</span>
          </div>
          <button
            className="sso-button"
            type="button"
            onClick={() => setErr("Google Workspace sign-in is not connected. Use your ForwardScan username and password.")}
          >
            <span>G</span>Sign in with Google Workspace
          </button>
          <p className="login-help">
            Having trouble signing in? <button type="button" onClick={() => setHelp(true)}>Contact your administrator</button>
          </p>
        </div>
        <footer className="login-footer">
          <span>© {new Date().getFullYear()} ForwardScan</span>
          <div>
            <span>Dispatch scanning</span>
          </div>
        </footer>
      </main>
    </div>
  );
}
