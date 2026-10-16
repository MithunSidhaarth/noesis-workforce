"use client";
import { useState } from "react";
import { Badge, Card, Drawer, ErrorNote, Json, PageHeader, Table } from "@/components/ui";
import { ago, useApi } from "@/lib/api";

export default function Messages() {
  const [q, setQ] = useState("");
  const [corr, setCorr] = useState("");
  const params = new URLSearchParams({ limit: "200", ...(q && { q }), ...(corr && { correlation: corr }) });
  const { data, error } = useApi<any[]>(`/messages?${params}`);
  const [sel, setSel] = useState<any>(null);
  return (
    <>
      <PageHeader title="Inter-agent messages" subtitle="Typed, persisted messages between bots. Click a correlation id to see the whole conversation chain." />
      <ErrorNote error={error} />
      <div className="mb-4 flex flex-wrap gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search payload or type…"
          className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700" />
        {corr && <button onClick={() => setCorr("")} className="rounded-md bg-zinc-100 px-2 text-xs dark:bg-zinc-800">chain {corr.slice(-8)} ✕</button>}
      </div>
      <Card>
        <Table rows={data} onRow={setSel} cols={[
          { key: "type", label: "Type", render: (m) => <span className="font-mono text-xs">{m.type}</span> },
          { key: "sender", label: "From" }, { key: "recipient", label: "To", render: (m) => m.recipient ?? `#${m.topic}` },
          { key: "status", label: "Status", render: (m) => <Badge>{m.status}</Badge> },
          { key: "hop_count", label: "Hops" },
          { key: "correlation_id", label: "Chain", render: (m) => (
            <button className="font-mono text-xs text-sky-600 hover:underline" onClick={(e) => { e.stopPropagation(); setCorr(m.correlation_id); }}>
              {m.correlation_id?.slice(-8)}</button>) },
          { key: "created_at", label: "When", render: (m) => ago(m.created_at) },
        ]} />
      </Card>
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel?.type}>
        {sel && <><p className="mb-2 text-sm">{sel.sender} → {sel.recipient ?? `#${sel.topic}`}</p>
          {sel.error && <p className="mb-2 text-sm text-rose-600">{sel.error}</p>}<Json value={sel.payload} /></>}
      </Drawer>
    </>
  );
}
