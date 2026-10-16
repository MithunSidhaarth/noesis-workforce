"use client";
import { useState } from "react";
import { Badge, Button, Card, ErrorNote, Json, PageHeader, Tabs, Empty, Skeleton } from "@/components/ui";
import { ago, post, useApi } from "@/lib/api";

export default function Approvals() {
  const [tab, setTab] = useState("pending");
  const { data, error, reload } = useApi<any[]>(`/approvals?status=${tab}`);
  return (
    <>
      <PageHeader title="Approvals" subtitle="Actions the policy engine held for your decision. Only you can grant or deny; bots cannot." />
      <ErrorNote error={error} />
      <Tabs tabs={["pending", "granted", "denied"]} value={tab} onChange={setTab} />
      {!data ? <Skeleton /> : !data.length ? <Empty>No {tab} approvals.</Empty> : (
        <div className="space-y-4">
          {data.map((a) => (
            <Card key={a.id} title={<span>{a.summary || a.action}</span>} action={<Badge>{a.status}</Badge>}>
              <div className="space-y-3 text-sm">
                <p className="text-zinc-500">Requested by <b>{a.bot_id}</b> · {a.action} · {ago(a.created_at)}</p>
                {!!a.reasons?.length && <ul className="list-disc pl-5 text-amber-800 dark:text-amber-300">{a.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>}
                <details><summary className="cursor-pointer text-xs text-zinc-500">Exact action payload</summary><Json value={a.payload} /></details>
                {a.status === "pending" && (
                  <div className="flex gap-2">
                    <Button variant="primary" onClick={() => post(`/approvals/${a.id}/grant`).then(reload)}>Approve</Button>
                    <Button variant="danger" onClick={() => post(`/approvals/${a.id}/deny`).then(reload)}>Deny</Button>
                  </div>
                )}
                {a.decided_by && <p className="text-xs text-zinc-500">Decided by {a.decided_by} {ago(a.decided_at)}</p>}
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
