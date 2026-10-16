"use client";
import { Badge, Button, Card, ErrorNote, Json, PageHeader, Table } from "@/components/ui";
import { fmtTime, post, useApi } from "@/lib/api";

export default function Settings() {
  const { data, error, reload } = useApi<any>("/settings");
  const set = (k: string, v: boolean) => post("/settings", { [k]: v }).then(reload);
  return (
    <>
      <PageHeader title="Settings" subtitle="Global switches and the policies bots run under. Policies are files on the server; bots can read them but never change them." />
      <ErrorNote error={error} />
      <div className="mb-6 grid gap-4 md:grid-cols-2">
        <Card title="Emergency stop">
          <p className="mb-3 text-sm text-zinc-500">Pausing stops every bot from starting new work and blocks all external actions.</p>
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
