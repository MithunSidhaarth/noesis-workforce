"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, post, useLive } from "@/lib/api";

const NAV: [string, string][] = [
  ["/", "Overview"], ["/bots", "Bots"], ["/tasks", "Tasks"], ["/messages", "Messages"],
  ["/opportunities", "Opportunities"], ["/applications", "Applications"], ["/content", "Content"],
  ["/email", "Email"], ["/outreach", "Outreach"], ["/knowledge", "Knowledge"],
  ["/approvals", "Approvals"], ["/logs", "Logs & audit"], ["/integrations", "Integrations"],
  ["/usage", "Usage"], ["/settings", "Settings"],
];

export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const live = useLive();
  const [flags, setFlags] = useState<{ paused: boolean; dry_run: boolean } | null>(null);

  useEffect(() => {
    if (path === "/login") return;
    api<{ authenticated: boolean }>("/auth/me").then((r) => {
      if (!r.authenticated) location.href = "/login";
      setAuthed(r.authenticated);
    });
    api("/settings").then((s) => setFlags({ paused: s.paused, dry_run: s.dry_run })).catch(() => {});
  }, [path]);
  useEffect(() => setOpen(false), [path]);

  if (path === "/login") return <>{children}</>;
  if (!authed) return <div className="p-8 text-sm text-zinc-500">Loading…</div>;

  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  return (
    <div className="min-h-screen lg:flex">
      <aside className={`${open ? "block" : "hidden"} fixed inset-y-0 z-30 w-60 shrink-0 overflow-y-auto border-r border-zinc-200 bg-white px-3 py-4 dark:border-zinc-800 dark:bg-zinc-900 lg:sticky lg:top-0 lg:block lg:h-screen`}>
        <div className="mb-6 px-2">
          <div className="text-sm font-bold tracking-widest text-zinc-900 dark:text-white">NOESIS</div>
          <div className="text-xs text-zinc-500">Autonomous workforce</div>
        </div>
        <nav className="space-y-0.5">
          {NAV.map(([href, label]) => (
            <Link key={href} href={href}
              className={`flex items-center justify-between rounded-md px-2 py-1.5 text-sm ${active(href) ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-white" : "text-zinc-600 hover:bg-zinc-50 dark:text-zinc-400 dark:hover:bg-zinc-800/60"}`}>
              {label}
              {href === "/approvals" && live?.approvals ? (
                <span className="rounded-full bg-amber-500 px-1.5 text-xs font-semibold text-white">{live.approvals}</span>
              ) : null}
            </Link>
          ))}
        </nav>
        <button onClick={async () => { await post("/auth/logout"); location.href = "/login"; }}
          className="mt-6 w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-500 hover:bg-zinc-50 dark:hover:bg-zinc-800">
          Sign out
        </button>
      </aside>
      <div className="min-w-0 flex-1">
        <div className="sticky top-0 z-20 flex items-center gap-3 border-b border-zinc-200 bg-white/80 px-4 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/80">
          <button className="lg:hidden" onClick={() => setOpen(!open)} aria-label="Menu">☰</button>
          <span className={`h-2 w-2 rounded-full ${live ? "bg-emerald-500" : "bg-zinc-300"}`} title={live ? "Live" : "Connecting"} />
          <span className="text-xs text-zinc-500">{live ? "Live" : "Connecting…"}</span>
          <div className="ml-auto flex gap-2 text-xs">
            {flags?.paused && <span className="rounded-md bg-amber-100 px-2 py-0.5 font-medium text-amber-800">All bots paused</span>}
            {flags?.dry_run && <span className="rounded-md bg-violet-100 px-2 py-0.5 font-medium text-violet-800">Dry-run</span>}
          </div>
        </div>
        <main className="mx-auto max-w-7xl p-4 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
