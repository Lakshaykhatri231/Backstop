import { getToken } from "../auth-token";

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    const detail =
      body && typeof body === "object" && "detail" in body ? String((body as { detail: unknown }).detail) : undefined;
    super(detail ?? `Request failed with status ${status}`);
    this.status = status;
    this.body = body;
  }
}

type ApiFetchOptions = {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  auth?: boolean; // attach Authorization header — defaults to true
};

// Relative paths only: in dev, Vite's server.proxy forwards these to the
// FastAPI backend so the browser sees a same-origin request (no CORS); in
// production, FastAPI serves this build itself, so it's same-origin for real.
export async function apiFetch<T>(path: string, opts: ApiFetchOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true } = opts;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(path, {
    method,
    headers,
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;

  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}
