"use client";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Badge, Card, ErrorNote, Json, PageHeader, Stat, Table } from "@/components/ui";
import { ago, fmtTime, useApi } from "@/lib/api";

function Workspace() {
  const id = useSearchParams().get("id");
  const { data, error } = useApi<any>(id ? `/bots/${id}` : null);
  if (!id) return <p>No bot selected.</p>;
  const b = data?.bot;
  return (
    <>
      <PageHeader title={b?.name ?? id} subtitle={b?.purpose} actions={b && <Badge>{b.enabled ? b.state : "disabled"}</Badge>} />
      <ErrorNote error={error} />
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Current task" value={<span className="text-base">{data?.current_task?.kind ?? "idle"}</span>} />
        <Stat label="Last heartbeat" value={<span className="text-base">{ago(b?.last_heartbeat)}</span>} />
        <Stat label="Model calls (30d)" value={data?.usage?.calls ?? 0} hint={`$${(data?.usage?.cost ?? 0).toFixed(4)} · ${data?.usage?.tokens ?? 0} tokens`} />
        <Stat label="OK / failed" value={b ? `${b.tasks_succeeded} / ${b.tasks_failed}` : "…"} />
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="Recent tasks">
          <Table rows={data?.recent_tasks} cols={[
            { key: "kind", label: "Kind" }, { key: "status", label: "Status", render: (t) => <Badge>{t.status}</Badge> },
            { key: "attempts", label: "Tries" }, { key: "created_at", label: "Created", render: (t) => ago(t.created_at) },
            { key: "error", label: "Error", render: (t) => <span className="text-xs text-rose-600">{t.error?.slice(0, 120)}</span> },
          ]} />
        </Card>
        <Card title="Schedules">
          <Table rows={data?.schedules} cols={[
            { key: "name", label: "Name" },
            { key: "when", label: "When", render: (s) => s.every_s ? `every ${Math.round(s.every_s / 60)} min` : `${s.at_time}${s.weekdays ? ` (${s.weekdays})` : ""}` },
            { key: "next_run", label: "Next", render: (s) => fmtTime(s.next_run) },
            { key: "last_run", label: "Last", render: (s) => ago(s.last_run) },
          ]} />
        </Card>
        <Card title="Messages">
          <Table rows={data?.messages} cols={[
            { key: "type", label: "Type", render: (m) => <span className="font-mono text-xs">{m.type}</span> },
            { key: "route", label: "Route", render: (m) => <span className="text-xs">{m.sender} → {m.recipient}</span> },
            { key: "status", label: "Status", render: (m) => <Badge>{m.status}</Badge> },
            { key: "created_at", label: "When", render: (m) => ago(m.created_at) },
          ]} />
        </Card>
        <Card title="External actions">
          <Table rows={data?.actions} rowKey="created_at" cols={[
            { key: "action", label: "Action" }, { key: "status", label: "Result", render: (a) => <Badge>{a.status}</Badge> },
            { key: "target", label: "Target", render: (a) => <span className="text-xs">{a.target}</span> },
            { key: "created_at", label: "When", render: (a) => ago(a.created_at) },
          ]} />
        </Card>
        <Card title="Recent errors">
          <Table rows={data?.errors} rowKey="ts" empty="No errors." cols={[
            { key: "ts", label: "When", render: (e) => ago(e.ts) }, { key: "message", label: "Message" },
          ]} />
        </Card>
        <Card title="Configuration (read-only)"><Json value={b?.config} /></Card>
      </div>
    </>
  );
}

export default function Page() {
  return <Suspense><Workspace /></Suspense>;
}
