"use client";
import { useMemo, useState } from "react";
import { Badge, Card, Drawer, ErrorNote, PageHeader, Table, Tabs } from "@/components/ui";
import { ago, fmtTime, until, useApi } from "@/lib/api";

export default function Opportunities() {
  const { data, error } = useApi<any[]>("/opportunities");
  const sources = useApi<any[]>("/sources");
  const [tab, setTab] = useState("all");
  const [sel, setSel] = useState<any>(null);
  const rows = useMemo(() => (data ?? []).filter((o) => tab === "all" || o.verification_status === tab || o.type === tab), [data, tab]);
  return (
    <>
      <PageHeader title="Opportunities" subtitle="Discovered by the Scout, verified by Quality. Unknown deadlines are shown as unknown, never guessed." />
      <ErrorNote error={error} />
      <Tabs tabs={["all", "verified", "needs_review", "failed", "hackathon", "internship", "scholarship", "fellowship"]} value={tab} onChange={setTab} />
      <Card className="mb-6">
        <Table rows={data ? rows : null} onRow={setSel} empty="No opportunities yet. The Scout sweeps sources every 15 minutes." cols={[
          { key: "title", label: "Opportunity", render: (o) => <div className="max-w-md"><div className="font-medium">{o.title}</div>
            <div className="text-xs text-zinc-500">{o.organizer ?? o.discovery_source}</div></div> },
          { key: "type", label: "Type" },
          { key: "deadline", label: "Deadline", render: (o) => o.deadline ? <span title={fmtTime(o.deadline)}>{until(o.deadline)}</span> : <span className="text-zinc-400">unknown</span> },
          { key: "relevance_score", label: "Score", render: (o) => o.relevance_score != null ? o.relevance_score.toFixed(2) : "—" },
          { key: "verification_status", label: "Verified", render: (o) => <Badge>{o.verification_status}</Badge> },
          { key: "eligibility_status", label: "Eligible", render: (o) => <Badge>{o.eligibility_status}</Badge> },
          { key: "application_status", label: "Application", render: (o) => <Badge>{o.application_status}</Badge> },
        ]} />
      </Card>
      <Card title="Sources">
        <Table rows={sources.data} cols={[
          { key: "name", label: "Source" }, { key: "kind", label: "Kind" },
          { key: "enabled", label: "Enabled", render: (s) => s.enabled ? "yes" : "no" },
          { key: "reliability", label: "Reliability" },
          { key: "consecutive_failures", label: "Failures", render: (s) => <span className={s.consecutive_failures ? "text-rose-600" : ""}>{s.consecutive_failures}</span> },
          { key: "last_success", label: "Last success", render: (s) => ago(s.last_success) },
          { key: "last_error", label: "Last error", render: (s) => <span className="text-xs text-rose-600">{s.last_error?.slice(0, 80)}</span> },
        ]} />
      </Card>
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel?.title}>
        {sel && (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap gap-2"><Badge>{sel.verification_status}</Badge><Badge>{sel.eligibility_status}</Badge><Badge>{sel.type}</Badge><Badge tone="gray">{sel.remote_status}</Badge></div>
            {sel.official_url && <a href={sel.official_url} target="_blank" rel="noopener noreferrer" className="break-all text-sky-600 hover:underline">{sel.official_url}</a>}
            <p><b>Deadline:</b> {sel.deadline ? `${fmtTime(sel.deadline)} (${sel.deadline_text ?? ""})` : "unknown — check the official page"}</p>
            {sel.relevance_rationale && <p><b>Why it fits:</b> {sel.relevance_rationale}</p>}
            {!!sel.eligibility?.length && <><b>Eligibility</b><ul className="list-disc pl-5">{sel.eligibility.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul></>}
            {!!sel.required_materials?.length && <><b>Materials</b><ul className="list-disc pl-5">{sel.required_materials.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul></>}
            {!!sel.verification_notes?.length && <><b>Verification notes</b><ul className="list-disc pl-5 text-zinc-600 dark:text-zinc-400">{sel.verification_notes.map((e: any, i: number) => <li key={i}>{typeof e === "string" ? e : JSON.stringify(e)}</li>)}</ul></>}
            {sel.note_path && <p className="text-xs text-zinc-500">Vault note: {sel.note_path}</p>}
          </div>
        )}
      </Drawer>
    </>
  );
}
