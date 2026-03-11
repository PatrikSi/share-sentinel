import { FormEvent, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { StatusBanner } from "@/components/status-banner";
import { getAccessToken, setTokens } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";

type RegistrationSettings = {
  allow_self_registration: boolean;
  password_min_length: number;
  password_require_lowercase: boolean;
  password_require_uppercase: boolean;
  password_require_number: boolean;
  password_require_special: boolean;
};

function resolveNextPath(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) {
    return "/projects";
  }
  return raw === "/" ? "/projects" : raw;
}

export function LoginPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const authenticated = !!getAccessToken();
  const next = new URLSearchParams(location.search).get("next");
  const nextPath = resolveNextPath(next);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [registerMode, setRegisterMode] = useState(false);
  const [registrationSettings, setRegistrationSettings] = useState<RegistrationSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  if (authenticated) {
    return <Navigate to={nextPath} replace />;
  }

  useEffect(() => {
    fetch(`${API_BASE}/auth/registration-settings`)
      .then(async (response) => {
        if (!response.ok) return;
        const data = await response.json();
        setRegistrationSettings(data as RegistrationSettings);
      })
      .catch(() => {
        setRegistrationSettings(null);
      });
  }, []);

  const allowRegistration = !!registrationSettings?.allow_self_registration;
  const passwordHints = useMemo(() => {
    if (!registrationSettings) return [];
    const hints = [`Minimum length ${registrationSettings.password_min_length}`];
    if (registrationSettings.password_require_lowercase) hints.push("Lowercase letter");
    if (registrationSettings.password_require_uppercase) hints.push("Uppercase letter");
    if (registrationSettings.password_require_number) hints.push("Number");
    if (registrationSettings.password_require_special) hints.push("Special character");
    return hints;
  }, [registrationSettings]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    setInfo(null);

    try {
      if (registerMode) {
        if (password !== confirmPassword) {
          throw new Error("Passwords do not match.");
        }
        const response = await fetch(`${API_BASE}/auth/register`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        setInfo("Registration submitted. A system administrator must approve the account before sign-in.");
        setRegisterMode(false);
        setPassword("");
        setConfirmPassword("");
      } else {
        const response = await fetch(`${API_BASE}/auth/login`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const data = await response.json();
        setTokens(data.access_token, data.refresh_token);
        localStorage.setItem("share_sentinel_theme", data.user.ui_theme);
        navigate(nextPath, { replace: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="mx-auto mt-16 grid max-w-6xl gap-6 lg:grid-cols-[minmax(0,1fr)_440px]">
      <div className="rounded-[32px] border border-slate-200 bg-[linear-gradient(145deg,rgba(255,255,255,0.98),rgba(226,232,240,0.88))] p-8 shadow-sm dark:border-slate-800 dark:bg-[linear-gradient(145deg,rgba(15,23,42,0.96),rgba(15,23,42,0.78))]">
        <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">share-sentinel</p>
        <h1 className="mt-3 max-w-xl text-4xl font-bold tracking-tight">Review share exposure, ingest collector output, and keep the audit trail visible.</h1>
        <p className="mt-4 max-w-2xl text-sm text-slate-600 dark:text-slate-300">
          The dashboard keeps project context, run intake, inventory review, and governance controls in one place. Sign in with an approved account to continue.
        </p>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <div className="rounded-3xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Dashboard</p>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Switch project context, monitor recent ingests, and jump into the latest run quickly.</p>
          </div>
          <div className="rounded-3xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Investigation</p>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Move between guided inventory filters, run diff review, and targeted search without losing context.</p>
          </div>
          <div className="rounded-3xl border border-white/70 bg-white/70 p-4 dark:border-slate-800 dark:bg-slate-950/40">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Governance</p>
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Sysadmins approve identities, manage tokens, and review audit activity from one control surface.</p>
          </div>
        </div>
      </div>

      <div className="panel rounded-[32px] p-8">
        <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">{registerMode ? "Registration" : "Authentication"}</p>
        <h2 className="mt-3 text-3xl font-bold tracking-tight">{registerMode ? "Create Account" : "Sign In"}</h2>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          {registerMode
            ? allowRegistration
              ? "Create an account request. A system administrator must approve it before you can sign in."
              : "Self-registration is disabled in this deployment."
            : "Use your approved Share Sentinel account to enter the dashboard."}
        </p>

        {allowRegistration ? (
          <button
            className="mt-4 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700 underline underline-offset-4 dark:text-emerald-300"
            type="button"
            onClick={() => {
              setRegisterMode((current) => !current);
              setError(null);
              setInfo(null);
            }}
          >
            {registerMode ? "Back to sign in" : "Request a new account"}
          </button>
        ) : (
          <p className="mt-4 text-xs text-slate-500">Account creation is handled by a system administrator.</p>
        )}

        <div className="mt-6 space-y-3">
          {error ? (
            <StatusBanner tone="error" title="Request Failed">
              <p>{error}</p>
            </StatusBanner>
          ) : null}
          {info ? (
            <StatusBanner tone="success" title="Request Accepted">
              <p>{info}</p>
            </StatusBanner>
          ) : null}
          {registerMode ? (
            <StatusBanner tone="info" title="Password Policy">
              <div className="flex flex-wrap gap-2 text-xs">
                {passwordHints.length > 0 ? passwordHints.map((hint) => <span className="rounded-full bg-white/70 px-3 py-1 dark:bg-slate-950/40" key={hint}>{hint}</span>) : <span>Must satisfy the current server password policy.</span>}
              </div>
            </StatusBanner>
          ) : null}
        </div>

        <form className="mt-6 space-y-4" onSubmit={onSubmit}>
          <label className="block text-sm">
            Email
            <input
              className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 dark:border-slate-700 dark:bg-slate-900"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="analyst@example.com"
              required
            />
          </label>
          <label className="block text-sm">
            {registerMode ? "Create password" : "Password"}
            <input
              className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 dark:border-slate-700 dark:bg-slate-900"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {registerMode ? (
            <label className="block text-sm">
              Confirm password
              <input
                className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 dark:border-slate-700 dark:bg-slate-900"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                required
              />
            </label>
          ) : null}
          <button
            className="w-full rounded-2xl bg-emerald-600 px-4 py-3 text-sm font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
            disabled={loading || (registerMode && !allowRegistration)}
            type="submit"
          >
            {loading ? (registerMode ? "Submitting account request..." : "Signing in...") : registerMode ? "Submit account request" : "Sign in"}
          </button>
        </form>
      </div>
    </section>
  );
}
