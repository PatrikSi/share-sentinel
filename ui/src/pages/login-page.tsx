import { FormEvent, useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { getAccessToken, setTokens } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";

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
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [allowRegistration, setAllowRegistration] = useState(false);
  const [registerMode, setRegisterMode] = useState(false);
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
        setAllowRegistration(!!data.allow_self_registration);
      })
      .catch(() => {
        setAllowRegistration(false);
      });
  }, []);

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
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        setInfo("Registration submitted. An admin must approve your account before login.");
        setRegisterMode(false);
        setPassword("");
        setConfirmPassword("");
      } else {
        const response = await fetch(`${API_BASE}/auth/login`, {
          method: "POST",
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
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="mx-auto mt-24 max-w-md panel">
      <h1 className="mb-2 text-3xl font-bold">Sign In</h1>
      <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">Use your Share Sentinel credentials.</p>
      {allowRegistration ? (
        <button
          className="mb-6 text-xs font-semibold text-ember underline underline-offset-4"
          type="button"
          onClick={() => {
            setRegisterMode((current) => !current);
            setError(null);
            setInfo(null);
          }}
        >
          {registerMode ? "Back to sign in" : "Create account"}
        </button>
      ) : (
        <div className="mb-6" />
      )}
      <form className="space-y-4" onSubmit={onSubmit}>
        <label className="block text-sm">
          Email
          <input
            className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </label>
        <label className="block text-sm">
          {registerMode ? "Create Password" : "Password"}
          <input
            className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {registerMode ? (
          <label className="block text-sm">
            Confirm Password
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              required
            />
          </label>
        ) : null}
        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        {info ? <p className="text-sm text-emerald-700 dark:text-emerald-300">{info}</p> : null}
        <button
          className="w-full rounded-xl bg-ember px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
          disabled={loading}
          type="submit"
        >
          {loading ? (registerMode ? "Submitting..." : "Signing in...") : registerMode ? "Create Account" : "Sign In"}
        </button>
      </form>
    </section>
  );
}
