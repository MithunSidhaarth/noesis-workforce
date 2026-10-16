"use client";
import { useState } from "react";
import { post } from "@/lib/api";

export default function Login() {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form className="w-full max-w-sm rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            await post("/auth/login", { password: pw });
            location.href = "/";
          } catch (e: any) {
            setErr(e.status === 429 ? "Too many attempts. Try again later." : "Incorrect password.");
          } finally {
            setBusy(false);
          }
        }}>
        <div className="mb-6">
          <div className="text-sm font-bold tracking-widest">NOESIS</div>
          <div className="text-xs text-zinc-500">Private control plane</div>
        </div>
        <label className="text-sm font-medium" htmlFor="pw">Password</label>
        <input id="pw" type="password" autoFocus autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)}
          className="mt-1 w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm dark:border-zinc-700" />
        {err && <p className="mt-2 text-sm text-rose-600">{err}</p>}
        <button disabled={busy || !pw} className="mt-4 w-full rounded-md bg-zinc-900 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-zinc-900">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
