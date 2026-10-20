import { config } from "./config";

export type ErrorData = {
  detail?: string;
  code?: string;
  errors?: Record<string, string[]>;
  inline_errors?: Record<string, InlineErrors>;
  row_errors?: Record<string, Record<string, string[]>>;
  plan?: unknown;
};
export type InlineErrors = { rows: Record<string, Record<string, string[]>>; non_form: string[] };

export class ApiError extends Error {
  status: number;
  data: ErrorData;

  constructor(status: number, data: ErrorData) {
    super(data?.detail ?? `HTTP ${status}`);
    this.status = status;
    this.data = data ?? {};
  }
}

export function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

/** JSON request; a ``FormData`` body is sent as multipart (file uploads). */
export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; params?: Record<string, string> } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const qs = options.params ? "?" + new URLSearchParams(options.params).toString() : "";
  const multipart = options.body instanceof FormData;
  const resp = await fetch(config.apiBase + path + qs, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options.body !== undefined && !multipart ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}),
    },
    body: multipart ? (options.body as FormData) : options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    if (resp.status === 401 && onUnauthorized && !path.startsWith("/auth/")) onUnauthorized();
    throw new ApiError(resp.status, data);
  }
  return data as T;
}

/** GET a file from the API and save it; returns the response headers. */
export async function download(path: string, params: Record<string, string>, fallbackName: string): Promise<Headers> {
  const qs = new URLSearchParams(params).toString();
  const resp = await fetch(`${config.apiBase}${path}${qs ? "?" + qs : ""}`, { credentials: "same-origin" });
  if (!resp.ok) throw new ApiError(resp.status, await resp.json().catch(() => ({})));
  const name = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await resp.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return resp.headers;
}

export const crudPath = (label: string, suffix = "") => {
  const [app, model] = label.split(".");
  return `/crud/${app}/${model}/${suffix}`;
};

export const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));
