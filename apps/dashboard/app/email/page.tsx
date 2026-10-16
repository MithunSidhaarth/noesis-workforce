"use client";
import { useState } from "react";
import { Badge, Button, Card, Drawer, ErrorNote, PageHeader, Table } from "@/components/ui";
import { fmtTime, post, useApi } from "@/lib/api";

export default function Email() {
  const { data, error, reload } = useApi<any[]>("/emails");
  const [sel, setSel] = useState<any>(null);
  return (
    <>
      <PageHeader title="Email" subtitle="Read and draft only by default. Sending needs the email policy and your approval." />
      <ErrorNote error={error} />
      <Card>
        <Table rows={data} onRow={setSel} empty="No email processed. Gmail is connected once you complete the OAuth step in the integration checklist." cols={[
          { key: "subject", label: "Subject", render: (m) => <div className="max-w-md"><div className="font-medium">{m.subject}</div><div className="text-xs text-zinc-500">{m.sender}</div></div> },
          { key: "category", label: "Category", render: (m) => <Badge tone="gray">{m.category}</Badge> },
          { key: "priority", label: "Priority", render: (m) => <Badge tone={m.priority === "high" ? "red" : m.priority === "medium" ? "amber" : "gray"}>{m.priority}</Badge> },
          { key: "state", label: "State", render: (m) => <Badge>{m.state}</Badge> },
          { key: "received_at", label: "Received", render: (m) => fmtTime(m.received_at) },
        ]} />
      </Card>
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel?.subject}>
        {sel && (
          <div className="space-y-3 text-sm">
            <p className="text-zinc-500">{sel.sender}</p>
            <p className="rounded-lg bg-zinc-50 p-3 dark:bg-zinc-950">{sel.snippet}</p>
            {!!sel.actions?.length && <><b>Suggested actions</b><ul className="list-disc pl-5">{sel.actions.map((a: any, i: number) => <li key={i}>{typeof a === "string" ? a : JSON.stringify(a)}</li>)}</ul></>}
            {sel.draft && <><b>Draft reply</b><div className="whitespace-pre-wrap rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">{sel.draft}</div>
              <Button variant="primary" onClick={async () => { await post(`/emails/${sel.id}/send`); setSel(null); reload(); }}>Request send (approval required)</Button></>}
          </div>
        )}
      </Drawer>
    </>
  );
}
