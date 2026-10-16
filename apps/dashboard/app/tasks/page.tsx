"use client";
import { useState } from "react";
import { Badge, Button, Card, Drawer, ErrorNote, Json, PageHeader, Table, Tabs } from "@/components/ui";
import { ago, api, fmtTime, post, useApi } from "@/lib/api";

const FILTERS = ["all", "queued", "running", "waiting_approval", "succeeded", "failed", "dead", "cancelled"];

export default function Tasks() {
  const [f, setF] = useState("all");
  const { data, error, reload } = useApi<any[]>(`/tasks?limit=200${f !== "all" ? `&status=${f}` : ""}`);
  const [sel, setSel] = useState<any>(null);
  return (
    <>
      <PageHeader title="Task queue" subtitle="Durable queue with leases, retries, dead-letter and dependencies." />
      <ErrorNote error={error} />
      <Tabs tabs={FILTERS} value={f} onChange={setF} />
      <Card>
        <Table rows={data} onRow={async (t) => setSel(await api(`/tasks/${t.id}`))} cols={[
          { key: "bot_id", label: "Bot" }, { key: "kind", label: "Kind", render: (t) => <span className="font-mono text-xs">{t.kind}</span> },
          { key: "status", label: "Status", render: (t) => <Badge>{t.status}</Badge> },
          { key: "attempts", label: "Tries", render: (t) => `${t.attempts}/${t.max_attempts}` },
          { key: "requested_by", label: "From", render: (t) => <span className="text-xs text-zinc-500">{t.requested_by}</span> },
          { key: "created_at", label: "Created", render: (t) => ago(t.created_at) },
          { key: "error", label: "Error", render: (t) => <span className="text-xs text-rose-600">{t.error?.slice(0, 100)}</span> },
        ]} />
      </Card>
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel ? `${sel.bot_id} · ${sel.kind}` : ""}>
        {sel && (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap gap-2">
              <Badge>{sel.status}</Badge>
              {["queued", "pending", "running", "waiting_approval", "failed"].includes(sel.status) &&
                <Button variant="danger" onClick={async () => { await post(`/tasks/${sel.id}/cancel`); setSel(await api(`/tasks/${sel.id}`)); reload(); }}>Cancel</Button>}
              {sel.status === "dead" &&
                <Button onClick={async () => { await post(`/tasks/${sel.id}/retry`); setSel(await api(`/tasks/${sel.id}`)); reload(); }}>Retry</Button>}
            </div>
            <dl className="grid grid-cols-2 gap-2 text-xs">
              <dt className="text-zinc-500">Created</dt><dd>{fmtTime(sel.created_at)}</dd>
              <dt className="text-zinc-500">Started</dt><dd>{fmtTime(sel.started_at)}</dd>
              <dt className="text-zinc-500">Finished</dt><dd>{fmtTime(sel.finished_at)}</dd>
              <dt className="text-zinc-500">Workflow</dt><dd className="font-mono">{sel.workflow_id ?? "—"}</dd>
              <dt className="text-zinc-500">Correlation</dt><dd className="font-mono">{sel.correlation_id}</dd>
              <dt className="text-zinc-500">Delegation depth</dt><dd>{sel.delegation_depth}</dd>
            </dl>
            {sel.error && <div className="rounded bg-rose-50 p-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-300">{sel.error}</div>}
            <h3 className="font-medium">Payload</h3><Json value={sel.payload} />
            <h3 className="font-medium">Result</h3><Json value={sel.result} />
            {sel.checkpoint && <><h3 className="font-medium">Checkpoint</h3><Json value={sel.checkpoint} /></>}
          </div>
        )}
      </Drawer>
    </>
  );
}
