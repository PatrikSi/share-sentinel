import { markSessionAnonymous, refreshSession } from "@/lib/auth";
import {
  BLOB_REQUEST_TIMEOUT_MS,
  boundedFetch,
  DEFAULT_REQUEST_TIMEOUT_MS,
  errorMessageFromBody,
  responseErrorMessage,
} from "@/lib/bounded-fetch";
import { API_BASE, CSRF_COOKIE_NAME, CSRF_HEADER_NAME } from "@/lib/runtime-config";

export { responseErrorMessage } from "@/lib/bounded-fetch";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const UPLOAD_TIMEOUT_MS = 4 * 60 * 60 * 1000;

function loginRedirectPath(): string {
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (!next || next === "/") {
    return "/";
  }
  return `/?next=${encodeURIComponent(next)}`;
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

  const response = await boundedFetch(
    `${API_BASE}${path}`,
    { ...init, headers, credentials: "include" },
    DEFAULT_REQUEST_TIMEOUT_MS,
  );
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

  const response = await boundedFetch(
    `${API_BASE}${path}`,
    { ...init, headers, credentials: "include" },
    BLOB_REQUEST_TIMEOUT_MS,
  );
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

export async function apiFetchAllPages<T>(buildPath: (cursor: string | null) => string, init: RequestInit = {}): Promise<T[]> {
  const items: T[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | null = null;

  const throwIfAborted = () => {
    if (!init.signal?.aborted) return;
    throw init.signal.reason instanceof Error
      ? init.signal.reason
      : new DOMException("Request cancelled.", "AbortError");
  };

  while (true) {
    throwIfAborted();
    if (cursor) {
      if (seenCursors.has(cursor)) {
        throw new Error("Pagination cursor repeated unexpectedly.");
      }
      seenCursors.add(cursor);
    }

    const data = await apiFetch(buildPath(cursor), init);
    throwIfAborted();
    items.push(...(((data?.items as T[]) || []) as T[]));
    cursor = (data?.next_cursor as string | null) || null;
    if (!cursor) {
      break;
    }
  }

  return items;
}

export async function apiUploadArtifact(
  path: string,
  artifact: Blob,
  options: {
    method?: "POST" | "PUT" | "PATCH";
    filename: string;
    contentType: "application/json" | "application/x-ndjson" | "application/gzip";
    onProgress?: (loaded: number, total: number) => void;
    signal?: AbortSignal;
  },
) {
  const method = options.method || "POST";
  if (
    options.filename.length === 0 ||
    options.filename.length > 255 ||
    options.filename.trim() !== options.filename ||
    /[\\/\x00-\x1f\x7f]/.test(options.filename)
  ) {
    throw new Error("Artifact transport filename is invalid.");
  }

  async function uploadOnce(allowRefresh: boolean): Promise<unknown> {
    const csrfToken = UNSAFE_METHODS.has(method) ? getCookieValue(CSRF_COOKIE_NAME) : null;

    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      let settled = false;

      const cleanup = () => options.signal?.removeEventListener("abort", abortUpload);
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const resolveOnce = (value: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      };
      const abortUpload = () => {
        if (settled) return;
        if (xhr.readyState === XMLHttpRequest.UNSENT || xhr.readyState === XMLHttpRequest.DONE) {
          rejectOnce(new DOMException("Upload cancelled.", "AbortError"));
        } else {
          xhr.abort();
        }
      };

      xhr.onabort = () => rejectOnce(new DOMException("Upload cancelled.", "AbortError"));
      options.signal?.addEventListener("abort", abortUpload, { once: true });
      if (options.signal?.aborted) {
        abortUpload();
        return;
      }

      xhr.open(method, `${API_BASE}${path}`);
      if (options.signal?.aborted) {
        abortUpload();
        return;
      }
      xhr.withCredentials = true;
      xhr.timeout = UPLOAD_TIMEOUT_MS;
      if (csrfToken) {
        xhr.setRequestHeader(CSRF_HEADER_NAME, csrfToken);
      }
      xhr.setRequestHeader("Content-Type", options.contentType);
      xhr.setRequestHeader("X-Artifact-Filename", options.filename);

      xhr.upload.onprogress = (event) => {
        options.onProgress?.(event.loaded, event.lengthComputable ? event.total : 0);
      };

      xhr.onerror = () =>
        rejectOnce(
          new Error(
            "Network error during upload. Delivery status is unknown; inspect the created run before retrying or deleting it.",
          ),
        );
      xhr.ontimeout = () =>
        rejectOnce(
          new Error(
            "Upload timed out after 4 hours. Delivery status is unknown; inspect the run before retrying or deleting it.",
          ),
        );
      xhr.onload = async () => {
        if (xhr.status === 401) {
          if (allowRefresh) {
            let refreshed = false;
            try {
              refreshed = await refreshAccessToken();
            } catch (error) {
              rejectOnce(error instanceof Error ? error : new Error("Session refresh failed during upload."));
              return;
            }
            if (refreshed) {
              if (options.signal?.aborted) {
                rejectOnce(new DOMException("Upload cancelled.", "AbortError"));
                return;
              }
              try {
                resolveOnce(await uploadOnce(false));
              } catch (error) {
                rejectOnce(error instanceof Error ? error : new Error("Upload retry failed."));
              }
              return;
            }
          }
          markSessionAnonymous();
          window.location.href = loginRedirectPath();
          rejectOnce(new Error("Unauthorized"));
          return;
        }

        if (xhr.status < 200 || xhr.status >= 300) {
          rejectOnce(
            new Error(
              errorMessageFromBody(xhr.responseText || "", xhr.status, xhr.getResponseHeader("x-request-id")),
            ),
          );
          return;
        }

        try {
          resolveOnce(parseResponseText(xhr.getResponseHeader("content-type") || "", xhr.responseText || ""));
        } catch (error) {
          rejectOnce(error instanceof Error ? error : new Error("Failed to parse upload response."));
        }
      };

      xhr.send(artifact);
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
