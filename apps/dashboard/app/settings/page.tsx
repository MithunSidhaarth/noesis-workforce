"use client";
import { Badge, Button, Card, ErrorNote, Json, PageHeader, Table } from "@/components/ui";
import { useState } from "react";
import { fmtTime, post, useApi } from "@/lib/api";

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
      <PageHeader title="Settings" subtitle="Global switches and the policies bots run under. Policies are files on the server; bots can read them but never change them." />
      <ErrorNote error={error} />
      <div className="mb-6 grid gap-4 md:grid-cols-2">
        <Card title="Emergency stop">
          <p className="mb-3 text-sm text-zinc-500">Pausing holds every external action (sends, posts, submissions) until you resume. Bots keep preparing work and you still get alerts.</p>
          {data && (data.paused
            ? <Button variant="primary" onClick={() => set("paused", false)}>Resume all bots</Button>
            : <Button variant="danger" onClick={() => set("paused", true)}>Pause all bots</Button>)}
        </Card>
        <Card title="Dry-run mode">
          <p className="mb-3 text-sm text-zinc-500">In dry-run, external actions are recorded but not performed (Telegram notifications to you and vault writes still happen).</p>
          {data && (data.dry_run
            ? <Button onClick={() => { if (confirm("Turn off dry-run? Actions allowed by policy will run for real.")) return set("dry_run", false); }}>Turn dry-run off</Button>
            : <Button variant="primary" onClick={() => set("dry_run", true)}>Turn dry-run on</Button>)}
          {data && <span className="ml-3"><Badge tone={data.dry_run ? "violet" : "amber"}>{data.dry_run ? "dry-run on" : "live"}</Badge></span>}
        </Card>
      </div>
      <Profile />
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
