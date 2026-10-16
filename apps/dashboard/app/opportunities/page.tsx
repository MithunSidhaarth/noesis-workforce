"use client";
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, Drawer, ErrorNote, PageHeader, Table, Tabs } from "@/components/ui";
import { ago, fmtTime, post, until, useApi } from "@/lib/api";

const now = () => Date.now() / 1000;
const isOpen = (o: any) => !["SUBMITTED", "CLOSED"].includes(o.application_status) && o.verification_status !== "failed"
  && (!o.deadline || o.deadline > now());

export default function Opportunities() {
  const { data, error, reload } = useApi<any[]>("/opportunities");
  const sources = useApi<any[]>("/sources");
  const [tab, setTab] = useState("shortlist");
  const [sel, setSel] = useState<any>(null);
  const [link, setLink] = useState("");
  const [reason, setReason] = useState("");
  useEffect(() => {
    const id = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("id") : null;
    if (id && data) setSel(data.find((o) => o.id === id) ?? null);
  }, [data]);
  const rows = useMemo(() => {
    const all = data ?? [];
    if (tab === "shortlist")
      return all.filter((o) => isOpen(o) && (o.confirmed_by || (o.relevance_score ?? 0) >= 0.5))
        .sort((a, b) => (a.deadline ?? 9e12) - (b.deadline ?? 9e12) || (b.relevance_score ?? 0) - (a.relevance_score ?? 0));
    return all.filter((o) => tab === "all" || o.verification_status === tab || o.type === tab);
  }, [data, tab]);
  const act = async (path: string, body?: unknown) => { await post(`/opportunities/${sel.id}/${path}`, body); setSel(null); setLink(""); setReason(""); await reload(); };
  return (
    <>
      <PageHeader title="Opportunities" subtitle="Discovered by the Scout, verified by Quality. Unknown deadlines are shown as unknown, never guessed." />
      <ErrorNote error={error} />
      <Tabs tabs={["shortlist", "all", "verified", "needs_review", "failed", "hackathon", "internship", "scholarship", "fellowship"]} value={tab} onChange={setTab} />
      <Card className="mb-6">
        <Table rows={data ? rows : null} onRow={setSel} empty={tab === "shortlist" ? "Nothing open on the shortlist. Check “all” for weaker matches." : "No opportunities yet. The Scout sweeps sources every 15 minutes."} cols={[
          { key: "title", label: "Opportunity", render: (o) => <div className="max-w-md"><div className="font-medium">{o.title}</div>
            <div className="text-xs text-zinc-500">{o.organizer ?? o.discovery_source}</div></div> },
          { key: "type", label: "Type" },
          { key: "deadline", label: "Deadline", render: (o) => o.deadline ? <span title={fmtTime(o.deadline)}>{until(o.deadline)}</span> : <span className="text-zinc-400">unknown</span> },
          { key: "relevance_score", label: "Score", render: (o) => o.relevance_score != null ? o.relevance_score.toFixed(2) : "—" },
          { key: "verification_status", label: "Verified", render: (o) => <Badge>{o.verification_status}</Badge> },
          { key: "eligibility_status", label: "Eligible", render: (o) => <Badge>{o.eligibility_status}</Badge> },
          { key: "application_status", label: "Application", render: (o) => <Badge>{o.application_status}</Badge> },
          { key: "confirmed_by", label: "You", render: (o) => o.confirmed_by ? <Badge tone="green">confirmed</Badge> : o.skip_reason ? <span className="text-xs text-zinc-500" title={o.skip_reason}>skipped</span> : "" },
        ]} />
      </Card>
      <Card title="Sources">
        <Table rows={sources.data} cols={[
          { key: "name", label: "Source" }, { key: "kind", label: "Kind" },
          { key: "enabled", label: "Enabled", render: (s) => s.enabled ? "yes" : "no" },
          { key: "reliability", label: "Reliability" },
          { key: "found", label: "Found" },
          { key: "verified", label: "Checked out", render: (s) => s.found ? `${s.verified ?? 0} (${Math.round(100 * (s.verified ?? 0) / s.found)}%)` : "—" },
          { key: "confirmed", label: "You confirmed", render: (s) => s.confirmed ?? 0 },
          { key: "applied", label: "Applied", render: (s) => s.applied ?? 0 },
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
            {!!sel.candidate_links?.length && sel.verification_status !== "verified" && <p><b>Possible official page (unconfirmed):</b> <a href={sel.candidate_links[0]} target="_blank" rel="noopener noreferrer" className="break-all text-sky-600 hover:underline">{sel.candidate_links[0]}</a></p>}
            {sel.skip_reason && <p className="text-zinc-500">You skipped this: {sel.skip_reason}</p>}
            {sel.note_path && <p className="text-xs text-zinc-500">Vault note: {sel.note_path}</p>}
            {isOpen(sel) && (
              <div className="space-y-3 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
                <p className="font-medium">Your call</p>
                <div className="flex flex-wrap items-center gap-2">
                  <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="Official link (optional), https://…"
                    className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-xs dark:border-zinc-700" />
                  <Button variant="primary" onClick={() => act("confirm", { url: link || null })}>✅ It's real, prepare it</Button>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button onClick={() => act("remind")} disabled={!sel.deadline}>⏰ Remind me T-7/T-2/T-1</Button>
                  <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why not? (helps the weekly review)"
                    className="min-w-0 flex-1 rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-xs dark:border-zinc-700" />
                  <Button variant="danger" onClick={() => act("skip", { reason })}>✖ Not for me</Button>
                </div>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </>
  );
}
