"use client";
import { useState } from "react";
import { Badge, Button, Card, Drawer, ErrorNote, PageHeader, Table } from "@/components/ui";
import { ago, fmtTime, post, until, useApi } from "@/lib/api";

const FLOW = ["DISCOVERED", "VERIFIED", "ELIGIBILITY_CHECK", "PREPARATION", "REVIEW", "READY_TO_SUBMIT", "SUBMITTED", "FOLLOW_UP", "CLOSED"];

export default function Applications() {
  const { data, error, reload } = useApi<any[]>("/applications");
  const [sel, setSel] = useState<any>(null);
  const [conf, setConf] = useState("");
  return (
    <>
      <PageHeader title="Applications" subtitle="Prepared only from verified facts. Web forms are left for you to submit; record the confirmation here afterwards." />
      <ErrorNote error={error} />
      <div className="mb-6 grid grid-cols-3 gap-2 text-center text-xs sm:grid-cols-5 lg:grid-cols-9">
        {FLOW.map((s) => (
          <div key={s} className="rounded-lg border border-zinc-200 bg-white p-2 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="text-lg font-semibold tabular-nums">{(data ?? []).filter((a) => a.state === s).length}</div>
            <div className="text-zinc-500">{s.replaceAll("_", " ").toLowerCase()}</div>
          </div>
        ))}
      </div>
      <Card>
        <Table rows={data} onRow={(a) => { setSel(a); setConf(""); }} empty="No applications yet. They appear once an opportunity is verified and evaluated." cols={[
          { key: "title", label: "Opportunity", render: (a) => <span className="font-medium">{a.title}</span> },
          { key: "state", label: "State", render: (a) => <Badge>{a.state}</Badge> },
          { key: "deadline", label: "Deadline", render: (a) => a.deadline ? until(a.deadline) : "unknown" },
          { key: "missing", label: "Missing", render: (a) => <span className="text-xs text-amber-700 dark:text-amber-400">{(a.missing ?? []).join(", ")}</span> },
          { key: "submission_method", label: "Method" },
          { key: "updated_at", label: "Updated", render: (a) => ago(a.updated_at) },
        ]} />
      </Card>
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel?.title}>
        {sel && (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap gap-2"><Badge>{sel.state}</Badge>{sel.submission_method && <Badge tone="gray">{sel.submission_method}</Badge>}</div>
            {sel.official_url && <a className="break-all text-sky-600 hover:underline" href={sel.official_url} target="_blank" rel="noopener noreferrer">{sel.official_url}</a>}
            <p>Deadline: {fmtTime(sel.deadline)}</p>
            <h3 className="font-medium">Prepared answers</h3>
            {Object.keys(sel.answers ?? {}).length ? Object.entries(sel.answers).map(([k, v]: any) => (
              <div key={k} className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
                <div className="mb-1 flex items-center justify-between"><b>{k}</b><Badge tone={v.status === "verified" ? "green" : "amber"}>{v.status}</Badge></div>
                <p className="whitespace-pre-wrap">{v.text}</p>
                <p className="mt-1 text-xs text-zinc-500">source: {v.source}</p>
              </div>
            )) : <p className="text-zinc-500">None yet.</p>}
            {!!sel.missing?.length && <p className="text-amber-700">Needs from you: {sel.missing.join(", ")}</p>}
            {(sel.state === "READY_TO_SUBMIT" || (sel.state === "REVIEW" && sel.submission_method === "web_form")) && (
              <div className="space-y-2 rounded-lg bg-zinc-50 p-3 dark:bg-zinc-950">
                {sel.submission_method === "web_form" ? (
                  <>
                    <p>Submit this on the official site yourself, then paste the confirmation number or email subject below.</p>
                    <input value={conf} onChange={(e) => setConf(e.target.value)} placeholder="Confirmation id / evidence"
                      className="w-full rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 dark:border-zinc-700" />
                    <Button variant="primary" disabled={conf.length < 2}
                      onClick={async () => { await post(`/applications/${sel.id}/submitted`, { confirmation: conf }); setSel(null); reload(); }}>
                      Record as submitted</Button>
                  </>
                ) : (
                  <Button variant="primary" onClick={async () => { await post(`/applications/${sel.id}/submit`); reload(); }}>
                    Submit (goes through approval policy)</Button>
                )}
              </div>
            )}
            {sel.evidence && <p className="text-xs">Evidence: {sel.evidence}</p>}
            {["SUBMITTED", "FOLLOW_UP"].includes(sel.state) && (
              <div className="flex flex-wrap items-center gap-2">
                <span>How did it go?</span>
                {["accepted", "rejected", "no_response"].map((o) => (
                  <Button key={o} variant={o === "accepted" ? "primary" : "default"}
                    onClick={async () => { await post(`/applications/${sel.id}/outcome`, { outcome: o }); setSel(null); reload(); }}>
                    {o.replace("_", " ")}</Button>
                ))}
              </div>
            )}
            {sel.outcome && <p>Outcome: <Badge>{sel.outcome}</Badge></p>}
          </div>
        )}
      </Drawer>
    </>
  );
}
