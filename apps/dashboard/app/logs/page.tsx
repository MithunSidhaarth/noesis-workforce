"use client";
import { useState } from "react";
import { Badge, Card, ErrorNote, PageHeader, Table, Tabs } from "@/components/ui";
import { fmtTime, useApi } from "@/lib/api";

export default function Logs() {
  const [tab, setTab] = useState("execution");
  const [level, setLevel] = useState("");
  const logs = useApi<any[]>(tab === "execution" ? `/logs?limit=300${level ? `&level=${level}` : ""}` : null);
  const audit = useApi<any[]>(tab === "audit" ? "/audit?limit=300" : null);
  const actions = useApi<any[]>(tab === "external_actions" ? "/actions?limit=300" : null);
  return (
    <>
      <PageHeader title="Logs & audit" subtitle="Execution logs, the append-only audit trail of policy and approval events, and every external action with its evidence." />
      <ErrorNote error={logs.error || audit.error || actions.error} />
      <Tabs tabs={["execution", "audit", "external_actions"]} value={tab} onChange={setTab} />
      {tab === "execution" && <>
        <div className="mb-3 flex gap-2 text-xs">{["", "info", "warning", "error"].map((l) => (
          <button key={l} onClick={() => setLevel(l)} className={`rounded px-2 py-1 ${level === l ? "bg-zinc-900 text-white dark:bg-white dark:text-zinc-900" : "bg-zinc-100 dark:bg-zinc-800"}`}>{l || "all"}</button>))}</div>
        <Card><Table rows={logs.data} cols={[
          { key: "ts", label: "Time", render: (l) => <span className="whitespace-nowrap text-xs">{fmtTime(l.ts)}</span> },
          { key: "level", label: "Level", render: (l) => <Badge>{l.level}</Badge> },
          { key: "bot_id", label: "Bot", render: (l) => l.bot_id ?? "system" },
          { key: "message", label: "Message", render: (l) => <span className="break-all">{l.message}</span> },
        ]} /></Card></>}
      {tab === "audit" && <Card><Table rows={audit.data} cols={[
        { key: "ts", label: "Time", render: (l) => <span className="whitespace-nowrap text-xs">{fmtTime(l.ts)}</span> },
        { key: "actor", label: "Actor" }, { key: "event", label: "Event", render: (l) => <span className="font-mono text-xs">{l.event}</span> },
        { key: "details", label: "Details", render: (l) => <span className="break-all text-xs text-zinc-500">{JSON.stringify(l.details)}</span> },
      ]} /></Card>}
      {tab === "external_actions" && <Card><Table rows={actions.data} cols={[
        { key: "created_at", label: "Time", render: (a) => <span className="whitespace-nowrap text-xs">{fmtTime(a.created_at)}</span> },
        { key: "bot_id", label: "Bot" }, { key: "action", label: "Action", render: (a) => <span className="font-mono text-xs">{a.action}</span> },
        { key: "status", label: "Result", render: (a) => <Badge>{a.status}</Badge> },
        { key: "target", label: "Target", render: (a) => <span className="text-xs">{a.target}</span> },
        { key: "evidence", label: "Evidence / error", render: (a) => <span className="break-all text-xs">{a.evidence ?? a.error}</span> },
      ]} /></Card>}
    </>
  );
}
