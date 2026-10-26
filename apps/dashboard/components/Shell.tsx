"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, post, useLive } from "@/lib/api";

// The five pages you use every day; everything else lives under "More".
const MAIN: [string, string, string][] = [
  ["/", "Home", "⌂"], ["/internships", "Internships", "◎"], ["/applications", "Applications", "▤"],
  ["/agents", "Agents", "✦"], ["/settings", "Settings", "⚙"],
];
const MORE: [string, string][] = [
  ["/prep", "Prep & people"], ["/approvals", "Approvals"], ["/bots", "Bots"], ["/tasks", "Tasks"], ["/messages", "Messages"],
  ["/content", "Content"], ["/email", "Email"], ["/outreach", "Outreach"], ["/knowledge", "Knowledge"],
  ["/opportunities", "All opportunities"], ["/logs", "Logs & audit"], ["/integrations", "Integrations"], ["/usage", "Usage"],
];

function ThemeToggle() {
  const [dark, setDark] = useState(false);
  useEffect(() => setDark(document.documentElement.classList.contains("dark")), []);
  return (
    <button aria-label="Toggle dark mode" title="Light / dark" className="rounded-md px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800"
      onClick={() => {
        const d = !dark;
        setDark(d);
        document.documentElement.classList.toggle("dark", d);
        try { localStorage.setItem("noesis-theme", d ? "dark" : "light"); } catch {}
      }}>
      {dark ? "☀" : "☾"}
    </button>
  );
}

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
  const item = (href: string, label: string, icon?: string) => (
    <Link key={href} href={href}
      className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${active(href) ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-white" : "text-zinc-600 hover:bg-zinc-50 dark:text-zinc-400 dark:hover:bg-zinc-800/60"}`}>
      {icon && <span className="w-4 text-center text-zinc-400">{icon}</span>}
      <span className="flex-1">{label}</span>
      {href === "/approvals" && live?.approvals ? (
        <span className="rounded-full bg-amber-500 px-1.5 text-xs font-semibold text-white">{live.approvals}</span>
      ) : null}
    </Link>
  );
  return (
    <div className="min-h-screen pb-16 lg:flex lg:pb-0">
      {open && <div className="fixed inset-0 z-20 bg-black/30 lg:hidden" onClick={() => setOpen(false)} />}
      <aside className={`${open ? "block" : "hidden"} fixed inset-y-0 z-30 w-60 shrink-0 overflow-y-auto border-r border-zinc-200 bg-white px-3 py-4 dark:border-zinc-800 dark:bg-zinc-900 lg:sticky lg:top-0 lg:block lg:h-screen`}>
        <div className="mb-6 px-2">
          <div className="text-sm font-bold tracking-widest text-zinc-900 dark:text-white">NOESIS</div>
          <div className="text-xs text-zinc-500">Internship engine · Bengaluru</div>
        </div>
        <nav className="space-y-0.5">{MAIN.map(([h, l, i]) => item(h, l, i))}</nav>
        <div className="mb-1 mt-6 px-2 text-xs font-medium uppercase tracking-wide text-zinc-400">More</div>
        <nav className="space-y-0.5">{MORE.map(([h, l]) => item(h, l))}</nav>
        <button onClick={async () => { await post("/auth/logout"); location.href = "/login"; }}
          className="mt-6 w-full rounded-md px-2 py-1.5 text-left text-sm text-zinc-500 hover:bg-zinc-50 dark:hover:bg-zinc-800">
          Sign out
        </button>
      </aside>
      <div className="min-w-0 flex-1">
        <div className="sticky top-0 z-10 flex items-center gap-3 border-b border-zinc-200 bg-white/80 px-4 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/80">
          <button className="lg:hidden" onClick={() => setOpen(!open)} aria-label="Menu">☰</button>
          <span className={`h-2 w-2 rounded-full ${live ? "bg-emerald-500" : "bg-zinc-300"}`} title={live ? "Live" : "Connecting"} />
          <span className="text-xs text-zinc-500">{live ? "Live" : "Connecting…"}</span>
          <div className="ml-auto flex items-center gap-2 text-xs">
            {flags?.paused && <span className="rounded-md bg-amber-100 px-2 py-0.5 font-medium text-amber-800">Paused</span>}
            {flags?.dry_run && <Link href="/settings" className="rounded-md bg-violet-100 px-2 py-0.5 font-medium text-violet-800">Test mode</Link>}
            <ThemeToggle />
          </div>
        </div>
        <main className="mx-auto max-w-7xl p-4 lg:p-8">{children}</main>
      </div>
      {/* phone: the five pages as a bottom tab bar */}
      <nav className="fixed inset-x-0 bottom-0 z-10 grid grid-cols-5 border-t border-zinc-200 bg-white/95 backdrop-blur dark:border-zinc-800 dark:bg-zinc-900/95 lg:hidden">
        {MAIN.map(([href, label, icon]) => (
          <Link key={href} href={href} className={`flex flex-col items-center py-2 text-[11px] ${active(href) ? "font-semibold text-zinc-900 dark:text-white" : "text-zinc-500"}`}>
            <span className="text-base leading-none">{icon}</span>{label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
