import { getAccessToken } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "http://localhost:8000";

export async function apiFetch(path: string, init: RequestInit = {}) {
  const token = getAccessToken();
  const headers = new Headers(init.headers || {});

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (response.status === 401) {
    throw new Error("Unauthorized");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed (${response.status})`);
  }

  const body = await response.text();
  return body ? JSON.parse(body) : null;
}
