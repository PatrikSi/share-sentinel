export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
export const BLOB_REQUEST_TIMEOUT_MS = 120_000;

const RETRY_SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const BODYLESS_STATUSES = new Set([101, 204, 205, 304]);

function appendRequestId(message: string, requestId: string | null): string {
  if (!requestId || message.includes(requestId)) {
    return message;
  }
  return `${message} (Request ID: ${requestId})`;
}

export function errorMessageFromBody(body: string, status: number, requestId: string | null = null): string {
  if (!body) return appendRequestId(`Request failed (${status}).`, requestId);
  if (body.trimStart().startsWith("<")) {
    return appendRequestId("API returned HTML instead of JSON. Check API routing and service health.", requestId);
  }
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object") {
      const payload = parsed as {
        detail?: unknown;
        request_id?: unknown;
        error?: { message?: unknown; request_id?: unknown };
      };
      const bodyRequestId =
        typeof payload.error?.request_id === "string"
          ? payload.error.request_id
          : typeof payload.request_id === "string"
            ? payload.request_id
            : null;
      const resolvedRequestId = requestId || bodyRequestId;
      if (typeof payload.detail === "string") return appendRequestId(payload.detail, resolvedRequestId);
      if (payload.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail)) {
        const detail = payload.detail as { code?: unknown; message?: unknown };
        if (typeof detail.message === "string") {
          const code = typeof detail.code === "string" ? ` (${detail.code})` : "";
          return appendRequestId(`${detail.message}${code}`, resolvedRequestId);
        }
      }
      if (typeof payload.error?.message === "string") {
        return appendRequestId(payload.error.message, resolvedRequestId);
      }
      return appendRequestId(`Request failed (${status}).`, resolvedRequestId);
    }
  } catch {
    // Fall through to the server-provided plain-text response.
  }
  return appendRequestId(body, requestId);
}

export async function responseErrorMessage(response: Pick<Response, "status" | "text" | "headers">): Promise<string> {
  const body = await response.text();
  return errorMessageFromBody(body, response.status, response.headers.get("x-request-id"));
}

export async function boundedFetch(input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS): Promise<Response> {
  const callerSignal = init.signal;
  const controller = new AbortController();
  const method = (init.method || "GET").toUpperCase();
  let timedOut = false;

  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(input, { ...init, signal: controller.signal });

    // Fetch resolves when response headers arrive. Buffer the body while the
    // same deadline is active so a proxy that stalls mid-response cannot leave
    // JSON, text, or blob consumers pending forever.
    const body = await response.arrayBuffer();
    return new Response(method === "HEAD" || BODYLESS_STATUSES.has(response.status) ? null : body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } catch (error) {
    if (timedOut) {
      const seconds = Math.max(1, Math.ceil(timeoutMs / 1000));
      if (RETRY_SAFE_METHODS.has(method)) {
        throw new Error(`API request timed out after ${seconds} seconds. It is safe to retry.`);
      }
      throw new Error(
        `API request timed out after ${seconds} seconds. The server may have completed the ${method} request; inspect current state before retrying.`,
      );
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}
