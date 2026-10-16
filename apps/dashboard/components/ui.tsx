"use client";
import { ReactNode, useState } from "react";

const TONES: Record<string, string> = {
  green: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-300",
  red: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-300",
  amber: "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-300",
  blue: "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-500/10 dark:text-sky-300",
  violet: "bg-violet-50 text-violet-700 ring-violet-600/20 dark:bg-violet-500/10 dark:text-violet-300",
  gray: "bg-zinc-100 text-zinc-600 ring-zinc-500/20 dark:bg-zinc-500/10 dark:text-zinc-300",
};

const STATUS_TONE: Record<string, keyof typeof TONES> = {
  succeeded: "green", ok: "green", idle: "green", verified: "green", processed: "green", granted: "green", PUBLISHED: "green",
  SUBMITTED: "green", sent: "green", eligible: "green",
  failed: "red", dead: "red", error: "red", revoked: "red", rejected: "red", BLOCKED: "red", ineligible: "red", denied: "red",
  blocked: "red",
  running: "blue", busy: "blue", queued: "blue", delivered: "blue", SCHEDULED: "blue", dry_run: "violet",
  waiting_approval: "amber", needs_review: "amber", pending: "amber", paused: "amber", approval_required: "amber",
  READY_TO_SUBMIT: "amber", needs_info: "amber", warning: "amber", not_configured: "gray", configured: "blue", offline: "gray",
};

export function Badge({ children, tone }: { children: ReactNode; tone?: keyof typeof TONES }) {
  const t = tone ?? STATUS_TONE[String(children)] ?? "gray";
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[t]}`}>
      {String(children).replaceAll("_", " ")}
    </span>
  );
}

export function Card({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900 ${className}`}>
      {title && (
        <header className="flex items-center justify-between gap-2 border-b border-zinc-100 px-4 py-3 dark:border-zinc-800">
          <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-100">{title}</h2>
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "red" | "amber" }) {
  const color = tone === "red" ? "text-rose-600" : tone === "amber" ? "text-amber-600" : "text-zinc-900 dark:text-zinc-50";
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-zinc-500">{hint}</div>}
    </div>
  );
}

export type Col<T> = { key: string; label: string; render?: (r: T) => ReactNode; className?: string };

export function Table<T extends Record<string, any>>({ rows, cols, empty = "Nothing here yet.", onRow, rowKey = "id" }:
  { rows: T[] | null | undefined; cols: Col<T>[]; empty?: string; onRow?: (r: T) => void; rowKey?: string }) {
  if (!rows) return <Skeleton />;
  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-100 text-left text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800">
            {cols.map((c) => (
              <th key={c.key} className={`px-4 py-2 font-medium ${c.className ?? ""}`}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {rows.map((r, i) => (
            <tr key={r[rowKey] ?? i} onClick={onRow ? () => onRow(r) : undefined}
                className={onRow ? "cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/50" : ""}>
              {cols.map((c) => (
                <td key={c.key} className={`px-4 py-2 align-top ${c.className ?? ""}`}>
                  {c.render ? c.render(r) : (r[c.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => (
  <div className="rounded-lg border border-dashed border-zinc-200 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700">{children}</div>
);

export const Skeleton = () => (
  <div className="space-y-2">
    {[0, 1, 2].map((i) => <div key={i} className="h-5 animate-pulse rounded bg-zinc-100 dark:bg-zinc-800" />)}
  </div>
);

export function ErrorNote({ error }: { error?: string | null }) {
  if (!error) return null;
  return <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-300">{error}</div>;
}

export function Button({ children, onClick, variant = "default", disabled, type = "button" }:
  { children: ReactNode; onClick?: () => Promise<unknown> | void; variant?: "default" | "primary" | "danger"; disabled?: boolean; type?: "button" | "submit" }) {
  const [busy, setBusy] = useState(false);
  const styles = {
    default: "bg-white text-zinc-800 ring-1 ring-inset ring-zinc-300 hover:bg-zinc-50 dark:bg-zinc-800 dark:text-zinc-100 dark:ring-zinc-700",
    primary: "bg-zinc-900 text-white hover:bg-zinc-700 dark:bg-white dark:text-zinc-900",
    danger: "bg-rose-600 text-white hover:bg-rose-500",
  }[variant];
  return (
    <button type={type} disabled={disabled || busy}
      onClick={onClick ? async (e) => { e.stopPropagation(); setBusy(true); try { await onClick(); } catch (err: any) { alert(err.message ?? err); } finally { setBusy(false); } } : undefined}
      className={`inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-xs font-medium shadow-sm transition disabled:opacity-50 ${styles}`}>
      {busy ? "…" : children}
    </button>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-50">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-zinc-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-96 overflow-auto rounded-lg bg-zinc-50 p-3 text-xs text-zinc-700 dark:bg-zinc-950 dark:text-zinc-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <div className="h-full w-full max-w-2xl overflow-y-auto bg-white p-6 shadow-xl dark:bg-zinc-900" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-start justify-between gap-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} className="text-zinc-400 hover:text-zinc-700" aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (t: string) => void }) {
  return (
    <div className="mb-4 flex flex-wrap gap-1 rounded-lg bg-zinc-100 p-1 text-sm dark:bg-zinc-800">
      {tabs.map((t) => (
        <button key={t} onClick={() => onChange(t)}
          className={`rounded-md px-3 py-1 ${value === t ? "bg-white font-medium shadow-sm dark:bg-zinc-900" : "text-zinc-600 dark:text-zinc-400"}`}>
          {t.replaceAll("_", " ")}
        </button>
      ))}
    </div>
  );
}
