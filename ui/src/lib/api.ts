import { markSessionAnonymous, refreshSession } from "@/lib/auth";

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

function appendRequestId(message: string, requestId: string | null): string {
  if (!requestId || message.includes(requestId)) {
    return message;
  }
  return `${message} (Request ID: ${requestId})`;
}

function toErrorMessage(body: string, status: number, requestId: string | null = null): string {
  if (!body) return appendRequestId(`Request failed (${status}).`, requestId);
  if (body.trimStart().startsWith("<")) {
    return appendRequestId("API returned HTML instead of JSON. Check API routing and service health.", requestId);
  }
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") return appendRequestId(parsed.detail, requestId);
  } catch {
    // fall through
  }
  return appendRequestId(body, requestId);
}

export async function responseErrorMessage(response: Pick<Response, "status" | "text" | "headers">): Promise<string> {
  const body = await response.text();
  return toErrorMessage(body, response.status, response.headers.get("x-request-id"));
}

function parseResponseText(contentType: string, body: string) {
  if (!body) return null;
  const normalizedContentType = contentType.toLowerCase();
  if (normalizedContentType.includes("application/json")) {
    return JSON.parse(body);
  }
  if (body.trimStart().startsWith("<")) {
    throw new Error("API returned HTML instead of JSON. Check API routing and service health.");
  }
  throw new Error(`Unexpected API response type (${normalizedContentType || "unknown"}).`);
}

function parseResponseBody(response: Response, body: string) {
  return parseResponseText(response.headers.get("content-type") || "", body);
}

function contentDispositionFilename(response: Response): string | null {
  const header = response.headers.get("content-disposition") || "";
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match) return decodeURIComponent(utf8Match[1]);
  const plainMatch = header.match(/filename="?([^\";]+)"?/i);
  if (plainMatch) return plainMatch[1];
  return null;
}

async function refreshAccessToken(): Promise<boolean> {
  return refreshSession();
}

export async function apiFetch(path: string, init: RequestInit = {}, allowRefresh = true) {
  const headers = new Headers(init.headers || {});
  const method = (init.method || "GET").toUpperCase();

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
    markSessionAnonymous();
    window.location.href = loginRedirectPath();
    throw new Error("Unauthorized");
  }
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }

  const body = await response.text();
  return parseResponseBody(response, body);
}

export async function apiFetchBlob(
  path: string,
  init: RequestInit = {},
  allowRefresh = true,
): Promise<{ blob: Blob; filename: string | null; contentType: string }> {
  const headers = new Headers(init.headers || {});
  const method = (init.method || "GET").toUpperCase();

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
        return apiFetchBlob(path, init, false);
      }
    }
    markSessionAnonymous();
    window.location.href = loginRedirectPath();
    throw new Error("Unauthorized");
  }
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }

  return {
    blob: await response.blob(),
    filename: contentDispositionFilename(response),
    contentType: response.headers.get("content-type") || "application/octet-stream",
  };
}

export async function apiFetchAllPages<T>(buildPath: (cursor: string | null) => string): Promise<T[]> {
  const items: T[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | null = null;

  while (true) {
    if (cursor) {
      if (seenCursors.has(cursor)) {
        throw new Error("Pagination cursor repeated unexpectedly.");
      }
      seenCursors.add(cursor);
    }

    const data = await apiFetch(buildPath(cursor));
    items.push(...(((data?.items as T[]) || []) as T[]));
    cursor = (data?.next_cursor as string | null) || null;
    if (!cursor) {
      break;
    }
  }

  return items;
}

export async function apiUploadFormData(
  path: string,
  formData: FormData,
  options: {
    method?: "POST" | "PUT" | "PATCH";
    onProgress?: (loaded: number, total: number) => void;
  } = {},
) {
  const method = options.method || "POST";

  async function uploadOnce(allowRefresh: boolean): Promise<unknown> {
    const csrfToken = UNSAFE_METHODS.has(method) ? getCookieValue(CSRF_COOKIE_NAME) : null;

    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, `${API_BASE}${path}`);
      xhr.withCredentials = true;
      if (csrfToken) {
        xhr.setRequestHeader(CSRF_HEADER_NAME, csrfToken);
      }

      xhr.upload.onprogress = (event) => {
        options.onProgress?.(event.loaded, event.lengthComputable ? event.total : 0);
      };

      xhr.onerror = () => reject(new Error("Network error during upload."));
      xhr.onload = async () => {
        if (xhr.status === 401) {
          if (allowRefresh) {
            const refreshed = await refreshAccessToken();
            if (refreshed) {
              try {
                resolve(await uploadOnce(false));
              } catch (error) {
                reject(error);
              }
              return;
            }
          }
          markSessionAnonymous();
          window.location.href = loginRedirectPath();
          reject(new Error("Unauthorized"));
          return;
        }

        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(toErrorMessage(xhr.responseText || "", xhr.status, xhr.getResponseHeader("x-request-id"))));
          return;
        }

        try {
          resolve(parseResponseText(xhr.getResponseHeader("content-type") || "", xhr.responseText || ""));
        } catch (error) {
          reject(error);
        }
      };

      xhr.send(formData);
    });
  }

  return uploadOnce(true);
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
