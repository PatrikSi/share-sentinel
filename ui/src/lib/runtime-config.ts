type ShareSentinelRuntimeConfig = {
  apiBase?: string;
  csrfCookieName?: string;
  csrfHeaderName?: string;
};

declare global {
  interface Window {
    __SHARE_SENTINEL_CONFIG__?: ShareSentinelRuntimeConfig;
  }
}

const runtimeConfig: ShareSentinelRuntimeConfig =
  typeof window === "undefined" ? {} : (window.__SHARE_SENTINEL_CONFIG__ ?? {});

export const API_BASE = runtimeConfig.apiBase || (import.meta.env.VITE_API_BASE_URL as string) || "/api";
export const CSRF_COOKIE_NAME =
  runtimeConfig.csrfCookieName || (import.meta.env.VITE_CSRF_COOKIE_NAME as string) || "share_sentinel_csrf";
export const CSRF_HEADER_NAME = (
  runtimeConfig.csrfHeaderName ||
  (import.meta.env.VITE_CSRF_HEADER_NAME as string) ||
  "x-csrf-token"
).toLowerCase();
