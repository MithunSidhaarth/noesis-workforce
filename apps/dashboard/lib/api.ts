"use client";
import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, init?: { method?: string; body?: unknown; form?: FormData }): Promise<T> {
  const method = init?.method ?? "GET";
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(method !== "GET" ? { "X-Noesis-Request": "1" } : {}),
      ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: init?.form ?? (init?.body !== undefined ? JSON.stringify(init.body) : undefined),
  });
  if (res.status === 401 && typeof window !== "undefined" && !location.pathname.startsWith("/login")) {
    location.href = "/login";
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {}
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export const post = <T = any>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body ?? {} });
export const patch = <T = any>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T = any>(path: string) => api<T>(path, { method: "DELETE" });
export const upload = <T = any>(path: string, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api<T>(path, { method: "POST", form });
};

/** Live event stream shared by every page (one EventSource per tab). */
type Live = { logs: any[]; audit: any[]; queue: Record<string, number>; bots: any[]; approvals: number };
const listeners = new Set<(e: Live) => void>();
let es: EventSource | null = null;
function ensureStream() {
  if (es || typeof window === "undefined") return;
  es = new EventSource("/api/events", { withCredentials: true });
  es.onmessage = (m) => {
    try {
      const data = JSON.parse(m.data) as Live;
      listeners.forEach((l) => l(data));
    } catch {}
  };
  es.onerror = () => {
    es?.close();
    es = null;
    setTimeout(ensureStream, 5000);
  };
}

export function useLive(cb?: (e: Live) => void) {
  const [last, setLast] = useState<Live | null>(null);
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    const l = (e: Live) => {
      setLast(e);
      ref.current?.(e);
    };
    listeners.add(l);
    ensureStream();
    return () => {
      listeners.delete(l);
    };
  }, []);
  return last;
}

/** Fetch + refetch when live events report new activity (no page reloads). */
export function useApi<T = any>(path: string | null, opts: { live?: boolean; interval?: number } = { live: true }) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);
  const lastRefresh = useRef(0);
  useLive((e) => {
    if (opts.live === false) return;
    if (!e.logs.length && !e.audit.length) return;
    if (Date.now() - lastRefresh.current < 1500) return;
    lastRefresh.current = Date.now();
    load();
  });
  return { data, error, loading, reload: load };
}

export const fmtTime = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }) : "—";

export function ago(ts?: number | null) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 0) return `in ${until(ts)}`;
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function until(ts?: number | null) {
  if (!ts) return "unknown";
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "passed";
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}
