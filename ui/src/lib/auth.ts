import { useSyncExternalStore } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api";
const CSRF_COOKIE_NAME = (import.meta.env.VITE_CSRF_COOKIE_NAME as string) || "share_sentinel_csrf";
const CSRF_HEADER_NAME = ((import.meta.env.VITE_CSRF_HEADER_NAME as string) || "x-csrf-token").toLowerCase();

export type SessionUser = {
  id: string;
  email: string;
  is_active: boolean;
  is_sysadmin: boolean;
  is_approved: boolean;
  approved_at: string | null;
  approved_by_user_id: string | null;
  ui_theme: string;
};

export type SessionSnapshot = {
  status: "unknown" | "authenticated" | "anonymous";
  user: SessionUser | null;
};

type Listener = () => void;

const listeners = new Set<Listener>();
let snapshot: SessionSnapshot = { status: "unknown", user: null };
let bootstrapPromise: Promise<SessionSnapshot> | null = null;
let refreshPromise: Promise<boolean> | null = null;

function emit(next: SessionSnapshot): void {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): SessionSnapshot {
  return snapshot;
}

function getCookieValue(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

async function fetchCurrentUser(): Promise<SessionUser | null> {
  const response = await fetch(`${API_BASE}/auth/me`, { credentials: "include" });
  if (!response.ok) return null;
  return (await response.json()) as SessionUser;
}

async function refreshSessionOnce(): Promise<boolean> {
  const headers = new Headers();
  const csrfToken = getCookieValue(CSRF_COOKIE_NAME);
  if (csrfToken) {
    headers.set(CSRF_HEADER_NAME, csrfToken);
  }

  const response = await fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
    headers,
  });
  return response.ok;
}

export async function refreshSession(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = refreshSessionOnce().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

export async function bootstrapSession(): Promise<SessionSnapshot> {
  if (snapshot.status !== "unknown") return snapshot;
  if (bootstrapPromise) return bootstrapPromise;

  bootstrapPromise = (async () => {
    try {
      let user = await fetchCurrentUser();
      if (!user && getCookieValue(CSRF_COOKIE_NAME)) {
        const refreshed = await refreshSession();
        if (refreshed) {
          user = await fetchCurrentUser();
        }
      }
      if (user) {
        markSessionAuthenticated(user);
      } else {
        markSessionAnonymous();
      }
    } catch {
      markSessionAnonymous();
    }
    return snapshot;
  })().finally(() => {
    bootstrapPromise = null;
  })();

  return bootstrapPromise;
}

export function useSession(): SessionSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export function markSessionAuthenticated(user: SessionUser): void {
  emit({ status: "authenticated", user });
}

export function markSessionAnonymous(): void {
  emit({ status: "anonymous", user: null });
}

export function resetSession(): void {
  emit({ status: "unknown", user: null });
}
