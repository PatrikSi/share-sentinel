import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { setTokens } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "http://localhost:8000";

export function LoginPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    try {
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
      navigate("/projects");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="mx-auto mt-24 max-w-md panel">
      <h1 className="mb-2 text-3xl font-bold">Sign In</h1>
      <p className="mb-6 text-sm text-slate-600 dark:text-slate-300">Use your Share Sentinel credentials.</p>
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
          Password
          <input
            className="mt-1 w-full rounded-xl border border-slate-300 bg-white/90 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error ? <p className="text-sm text-red-600">{error}</p> : null}
        <button
          className="w-full rounded-xl bg-ember px-4 py-2 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
          disabled={loading}
          type="submit"
        >
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </form>
    </section>
  );
}
