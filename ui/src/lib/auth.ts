import { useSyncExternalStore } from "react";

import { boundedFetch, responseErrorMessage } from "@/lib/bounded-fetch";
import { API_BASE, CSRF_COOKIE_NAME, CSRF_HEADER_NAME } from "@/lib/runtime-config";

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
  status: "unknown" | "authenticated" | "anonymous" | "error";
  user: SessionUser | null;
  error: string | null;
};

type Listener = () => void;

const listeners = new Set<Listener>();
let snapshot: SessionSnapshot = { status: "unknown", user: null, error: null };
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
  const response = await boundedFetch(`${API_BASE}/auth/me`, { credentials: "include" });
  if (response.status === 401 || response.status === 403) return null;
  if (!response.ok) throw new Error(await responseErrorMessage(response));
  return (await response.json()) as SessionUser;
}

async function refreshSessionOnce(): Promise<boolean> {
  const headers = new Headers();
  const csrfToken = getCookieValue(CSRF_COOKIE_NAME);
  if (csrfToken) {
    headers.set(CSRF_HEADER_NAME, csrfToken);
  }

  const response = await boundedFetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
    headers,
  });
  if (response.status === 401 || response.status === 403) return false;
  if (!response.ok) throw new Error(await responseErrorMessage(response));
  return true;
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

  const promise = (async () => {
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
    } catch (error) {
      markSessionError(error instanceof Error ? error.message : "The session service is unavailable.");
    }
    return snapshot;
  })().finally(() => {
    bootstrapPromise = null;
  });

  bootstrapPromise = promise;
  return promise;
}

export function useSession(): SessionSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export function markSessionAuthenticated(user: SessionUser): void {
  emit({ status: "authenticated", user, error: null });
}

export function markSessionAnonymous(): void {
  emit({ status: "anonymous", user: null, error: null });
}

export function markSessionError(message: string): void {
  emit({ status: "error", user: null, error: message });
}

export async function logoutSession(): Promise<void> {
  const headers = new Headers();
  const csrfToken = getCookieValue(CSRF_COOKIE_NAME);
  if (csrfToken) {
    headers.set(CSRF_HEADER_NAME, csrfToken);
  }
  const response = await boundedFetch(`${API_BASE}/auth/logout`, {
    method: "POST",
    credentials: "include",
    headers,
  });
  if (response.ok || response.status === 401) return;
  throw new Error(await responseErrorMessage(response));
}

export function resetSession(): void {
  emit({ status: "unknown", user: null, error: null });
}
