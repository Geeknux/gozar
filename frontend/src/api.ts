import { config } from "./config";

export class ApiError extends Error {
  status: number;
  data: { detail?: string; errors?: Record<string, string[]>; plan?: unknown };

  constructor(status: number, data: ApiError["data"]) {
    super(data?.detail ?? `HTTP ${status}`);
    this.status = status;
    this.data = data ?? {};
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; params?: Record<string, string> } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const qs = options.params ? "?" + new URLSearchParams(options.params).toString() : "";
  const resp = await fetch(config.apiBase + path + qs, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    if (resp.status === 401 && onUnauthorized && !path.startsWith("/auth/")) onUnauthorized();
    throw new ApiError(resp.status, data);
  }
  return data as T;
}

export const crudPath = (label: string, suffix = "") => {
  const [app, model] = label.split(".");
  return `/crud/${app}/${model}/${suffix}`;
};
