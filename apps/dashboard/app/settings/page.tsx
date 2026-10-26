"use client";
import { Badge, Button, Card, ErrorNote, Json, PageHeader, Table } from "@/components/ui";
import { useState } from "react";
import { ago, fmtTime, post, upload, useApi } from "@/lib/api";

function Resume() {
  const { data, error, reload } = useApi<any>("/resume", { live: false });
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <Card title="Resume" className="mb-6" action={data?.active && <a className="text-xs text-sky-600 hover:underline" href="/api/resume/pdf">One-page PDF ↓</a>}>
      <ErrorNote error={error} />
      <p className="mb-3 text-sm text-zinc-500">Used for keyword (ATS) scores and tailored bullets. Tailored bullets may only rephrase lines that are in it. PDF, .txt or .md, under 5 MB.</p>
      {data?.active ? <p className="mb-3 text-sm">Active: <b>{data.active.version}</b> · {data.active.filename} · {data.active.chars} characters · uploaded {ago(data.active.uploaded_at)}</p>
        : <p className="mb-3 text-sm text-amber-700">No resume yet.</p>}
      <input type="file" accept=".pdf,.txt,.md" className="text-sm" onChange={async (e) => {
        const f = e.target.files?.[0];
        if (!f) return;
        try { const r = await upload("/resume", f); setMsg(`Saved as ${r.version}.`); reload(); } catch (x: any) { setMsg(x.message); }
        e.target.value = "";
      }} />
      {msg && <p className="mt-2 text-xs text-zinc-500">{msg}</p>}
    </Card>
  );
}

function PhoneAndCalendar() {
  const { data, reload } = useApi<any>("/phone");
  const [note, setNote] = useState<string | null>(null);
  const p = data?.policy;
  return (
    <Card title="Phone calls and calendar" className="mb-6">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="text-sm">
          <div className="mb-1 flex items-center gap-2 font-medium">Calls to you <Badge tone={p?.enabled ? "green" : "gray"}>{p?.enabled ? "on" : "off"}</Badge>
            {data?.integration && <Badge>{data.integration.status}</Badge>}</div>
          <p className="text-zinc-500">An hour before an interview, on the last day of an application you haven't sent, or when an agent with “call me” asks. Uses the call-me plugin's CALLME_* settings; only ever your own number.
            Quiet hours {p?.quiet_hours ?? "—"}, at most {p?.max_calls_per_day ?? "—"} a day. Edit <code>policies/phone.yaml</code> to change.</p>
          {data?.integration?.detail && <p className="mt-1 text-xs text-zinc-500">{data.integration.detail}</p>}
          <div className="mt-2 flex items-center gap-2"><Button onClick={async () => { await post("/phone/test"); setNote("Test requested: the result arrives on Telegram."); reload(); }}>Test call</Button>
            {note && <span className="text-xs text-zinc-500">{note}</span>}</div>
          {!!data?.calls?.length && <ul className="mt-2 space-y-1 text-xs text-zinc-500">{data.calls.slice(0, 5).map((c: any) => (
            <li key={c.id}>{fmtTime(c.created_at)} · {c.reason} · <Badge>{c.status}</Badge>{c.error ? ` ${c.error}` : ""}</li>))}</ul>}
        </div>
        <div className="text-sm">
          <div className="mb-1 flex items-center gap-2 font-medium">Google Calendar sync <Badge tone={data?.calendar?.sync_enabled ? "green" : "gray"}>{data?.calendar?.sync_enabled ? "on" : "off"}</Badge></div>
          <p className="text-zinc-500">Deadlines, OAs and interviews go on your own calendar, without guests. Re-run <code>scripts/google_oauth.py</code> once (it now asks for calendar access), then set <code>sync_enabled: true</code> in <code>policies/calendar.yaml</code>.</p>
        </div>
      </div>
    </Card>
  );
}

function Profile() {
  const { data, error, reload } = useApi<any>("/profile");
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  return (
    <Card title="Your profile" className="mb-6">
      <span id="profile" />
      <ErrorNote error={error} />
      <p className="mb-3 text-sm text-zinc-500">Applications only use facts marked verified. Facts you add here (or with <code>/profile set</code> on Telegram) count as verified because you typed them. Sensitive values are masked.</p>
      {!!data?.problems?.length && <ul className="mb-3 list-disc pl-5 text-sm text-amber-700 dark:text-amber-400">{data.problems.map((p: string) => <li key={p}>{p}</li>)}</ul>}
      {!!data?.missing?.length && (
        <div className="mb-3 text-sm"><b>Applications are waiting on:</b>
          <ul className="list-disc pl-5">{data.missing.map((m: any) => <li key={m.item}>{m.item} ({m.applications}): <span className="text-zinc-500">{m.how}</span></li>)}</ul>
        </div>
      )}
      <Table rows={data?.facts} rowKey="key" empty="No facts yet." cols={[
        { key: "key", label: "Key" }, { key: "section", label: "Section" }, { key: "value", label: "Value" },
        { key: "verified", label: "Verified", render: (f) => f.verified ? <Badge tone="green">yes</Badge> : <Badge>no</Badge> },
        { key: "source", label: "From" },
      ]} />
      <form className="mt-3 flex flex-wrap gap-2" onSubmit={async (e) => { e.preventDefault(); await post("/profile", { key, value }); setKey(""); setValue(""); reload(); }}>
        <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="key, e.g. graduation_year"
          className="w-48 rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-xs dark:border-zinc-700" />
        <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="value"
          className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-xs dark:border-zinc-700" />
        <Button type="submit" variant="primary" disabled={!key || !value}>Save fact</Button>
      </form>
    </Card>
  );
}

export default function Settings() {
  const { data, error, reload } = useApi<any>("/settings");
  const set = (k: string, v: boolean) => post("/settings", { [k]: v }).then(reload);
  return (
    <>
      <PageHeader title="Settings" subtitle="Test mode, your resume and profile, calls and calendar, and the policies bots run under (files on the server that bots can read but never change)." />
      <ErrorNote error={error} />
      <div className="mb-6 grid gap-4 md:grid-cols-2">
        <Card title="Emergency stop">
          <p className="mb-3 text-sm text-zinc-500">Pausing holds every external action (sends, posts, submissions) until you resume. Bots keep preparing work and you still get alerts.</p>
          {data && (data.paused
            ? <Button variant="primary" onClick={() => set("paused", false)}>Resume all bots</Button>
            : <Button variant="danger" onClick={() => set("paused", true)}>Pause all bots</Button>)}
        </Card>
        <Card title="Test mode (dry-run)">
          <p className="mb-3 text-sm text-zinc-500">In dry-run, external actions are recorded but not performed (Telegram notifications to you and vault writes still happen).</p>
          {data && (data.dry_run
            ? <Button onClick={() => { if (confirm("Turn off dry-run? Actions allowed by policy will run for real.")) return set("dry_run", false); }}>Turn dry-run off</Button>
            : <Button variant="primary" onClick={() => set("dry_run", true)}>Turn dry-run on</Button>)}
          {data && <span className="ml-3"><Badge tone={data.dry_run ? "violet" : "amber"}>{data.dry_run ? "dry-run on" : "live"}</Badge></span>}
        </Card>
      </div>
      <Resume />
      <Profile />
      <PhoneAndCalendar />
      <Card title="Schedules" className="mb-6">
        <Table rows={data?.schedules} cols={[
          { key: "bot_id", label: "Bot" }, { key: "name", label: "Schedule" },
          { key: "when", label: "When", render: (s) => s.every_s ? `every ${Math.round(s.every_s / 60)} min` : `${s.at_time} ${s.weekdays ?? "daily"} (${s.tz})` },
          { key: "next_run", label: "Next run", render: (s) => fmtTime(s.next_run) },
        ]} />
      </Card>
      <Card title="Policies (read-only here)">
        <p className="mb-3 text-sm text-zinc-500">Vault path configured: {data?.vault_path_configured ? "yes" : "no"}. Edit files under <code>policies/</code> on the server to change these.</p>
        <Json value={data?.policies} />
      </Card>
    </>
  );
}
