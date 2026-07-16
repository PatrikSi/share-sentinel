import { FormEvent, useEffect, useMemo, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { StatusBanner } from "@/components/status-banner";
import { responseErrorMessage } from "@/lib/api";
import { markSessionAuthenticated, SessionUser, useSession } from "@/lib/auth";
import { API_BASE } from "@/lib/runtime-config";

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
  const session = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const next = new URLSearchParams(location.search).get("next");
  const nextPath = resolveNextPath(next);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [registerMode, setRegisterMode] = useState(false);
  const [registrationSettings, setRegistrationSettings] = useState<RegistrationSettings | null>(null);
  const [registrationWarning, setRegistrationWarning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  if (session.status === "authenticated") {
    return <Navigate to={nextPath} replace />;
  }

  useEffect(() => {
    fetch(`${API_BASE}/auth/registration-settings`)
      .then(async (response) => {
        if (!response.ok) {
          setRegistrationWarning(await responseErrorMessage(response));
          return;
        }
        const data = await response.json();
        setRegistrationSettings(data as RegistrationSettings);
        setRegistrationWarning(null);
      })
      .catch(() => {
        setRegistrationSettings(null);
        setRegistrationWarning("Registration options could not be loaded. Sign-in is still available.");
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
          throw new Error(await responseErrorMessage(response));
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
          throw new Error(await responseErrorMessage(response));
        }
        const data = await response.json();
        const user = data.user as SessionUser;
        markSessionAuthenticated(user);
        localStorage.setItem("share_sentinel_theme", user.ui_theme);
        navigate(nextPath, { replace: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="mx-auto mt-16 max-w-md">
      <div className="panel rounded-[32px] p-8">
        <h1 className="text-3xl font-bold tracking-tight">{registerMode ? "Create Account" : "Sign In"}</h1>

        {allowRegistration ? (
          <button
            className="mt-3 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700 underline underline-offset-4 dark:text-emerald-300"
            type="button"
            onClick={() => {
              setRegisterMode((current) => !current);
              setError(null);
              setInfo(null);
            }}
          >
            {registerMode ? "Back to sign in" : "Request access"}
          </button>
        ) : null}

        <div className="mt-5 space-y-3">
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
          {registrationWarning ? (
            <StatusBanner tone="warning" title="Registration Settings Unavailable">
              <p>{registrationWarning}</p>
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

        <form className="mt-5 space-y-4" onSubmit={onSubmit}>
          <label className="block text-sm">
            Email
            <input
              className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-slate-900 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500"
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
              className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
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
                className="mt-1 w-full rounded-2xl border border-slate-300 bg-white/90 px-3 py-3 text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
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
