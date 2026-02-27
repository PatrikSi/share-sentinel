import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";

function loginRedirectPath(): string {
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (!next || next === "/") {
    return "/";
  }
  return `/?next=${encodeURIComponent(next)}`;
}

function toErrorMessage(body: string, status: number): string {
  if (!body) return `Request failed (${status})`;
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // fall through
  }
  return body;
}

async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;

  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  const access = data.access_token as string;
  const nextRefresh = (data.refresh_token as string | undefined) || refreshToken;
  setTokens(access, nextRefresh);
  return access;
}

export async function apiFetch(path: string, init: RequestInit = {}, allowRefresh = true) {
  const token = getAccessToken();
  const headers = new Headers(init.headers || {});

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const shouldSetJsonContentType =
    !!init.body &&
    !headers.has("Content-Type") &&
    !(typeof FormData !== "undefined" && init.body instanceof FormData) &&
    !(typeof Blob !== "undefined" && init.body instanceof Blob);
  if (shouldSetJsonContentType) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (response.status === 401) {
    if (allowRefresh) {
      const refreshed = await refreshAccessToken();
      if (refreshed) {
        return apiFetch(path, init, false);
      }
    }
    clearTokens();
    window.location.href = loginRedirectPath();
    throw new Error("Unauthorized");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(toErrorMessage(body, response.status));
  }

  const body = await response.text();
  return body ? JSON.parse(body) : null;
}
