import { clearTokens, getAccessToken, getRefreshToken, setTokens } from "@/lib/auth";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";
const CSRF_COOKIE_NAME = (import.meta.env.VITE_CSRF_COOKIE_NAME as string) || "share_sentinel_csrf";
const CSRF_HEADER_NAME = ((import.meta.env.VITE_CSRF_HEADER_NAME as string) || "x-csrf-token").toLowerCase();
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

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

function parseResponseBody(response: Response, body: string) {
  if (!body) return null;
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  if (contentType.includes("application/json")) {
    return JSON.parse(body);
  }
  if (body.trimStart().startsWith("<")) {
    throw new Error("API returned HTML instead of JSON. Check API routing and service health.");
  }
  throw new Error(`Unexpected API response type (${contentType || "unknown"}).`);
}

async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;

  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
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
  const method = (init.method || "GET").toUpperCase();

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
  if (UNSAFE_METHODS.has(method) && !headers.has(CSRF_HEADER_NAME)) {
    const csrfToken = getCookieValue(CSRF_COOKIE_NAME);
    if (csrfToken) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
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
  return parseResponseBody(response, body);
}

function getCookieValue(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  const parts = document.cookie.split(";");
  for (const part of parts) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}
