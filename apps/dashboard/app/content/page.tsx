"use client";
import { useMemo, useState } from "react";
import { Badge, Card, Drawer, ErrorNote, Json, PageHeader, Table, Tabs } from "@/components/ui";
import { ago, fmtTime, useApi } from "@/lib/api";

export default function Content() {
  const { data, error } = useApi<any[]>("/content");
  const [view, setView] = useState("queue");
  const [sel, setSel] = useState<any>(null);
  const days = useMemo(() => {
    const out: { label: string; items: any[] }[] = [];
    for (let i = 0; i < 14; i++) {
      const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + i);
      const start = d.getTime() / 1000;
      out.push({ label: d.toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }),
        items: (data ?? []).filter((c) => c.scheduled_for && c.scheduled_for >= start && c.scheduled_for < start + 86400) });
    }
    return out;
  }, [data]);
  return (
    <>
      <PageHeader title="Content" subtitle="Plans, drafts, renders and publications. Publishing is off until you enable it in policy." />
      <ErrorNote error={error} />
      <Tabs tabs={["queue", "calendar"]} value={view} onChange={setView} />
      {view === "queue" ? (
        <Card>
          <Table rows={data} onRow={setSel} empty="No content yet. The Strategist plans each Monday from your captured knowledge." cols={[
            { key: "title", label: "Title", render: (c) => <span className="font-medium">{c.title}</span> },
            { key: "platform", label: "Platform" }, { key: "format", label: "Format" },
            { key: "state", label: "State", render: (c) => <Badge>{c.state}</Badge> },
            { key: "scheduled_for", label: "Scheduled", render: (c) => fmtTime(c.scheduled_for) },
            { key: "publication_url", label: "Published", render: (c) => c.publication_url ? <a className="text-sky-600 hover:underline" href={c.publication_url} target="_blank" rel="noopener noreferrer">link</a> : "—" },
          ]} />
        </Card>
      ) : (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-7">
          {days.map((d) => (
            <div key={d.label} className="min-h-28 rounded-lg border border-zinc-200 bg-white p-2 text-xs dark:border-zinc-800 dark:bg-zinc-900">
              <div className="mb-1 font-medium text-zinc-500">{d.label}</div>
              {d.items.map((c) => (
                <button key={c.id} onClick={() => setSel(c)} className="mb-1 block w-full truncate rounded bg-sky-50 px-1.5 py-1 text-left text-sky-800 dark:bg-sky-500/10 dark:text-sky-300">
                  {c.platform}: {c.title}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
      <Drawer open={!!sel} onClose={() => setSel(null)} title={sel?.title}>
        {sel && (
          <div className="space-y-3 text-sm">
            <div className="flex gap-2"><Badge>{sel.state}</Badge><Badge tone="gray">{sel.platform}</Badge><Badge tone="gray">{sel.format}</Badge></div>
            {sel.body && <div className="whitespace-pre-wrap rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">{sel.body}</div>}
            {!!sel.sources?.length && <><b>Sources</b><ul className="list-disc pl-5">{sel.sources.map((s: any, i: number) => <li key={i} className="break-all">{typeof s === "string" ? s : s.url ?? JSON.stringify(s)}</li>)}</ul></>}
            {!!sel.assets?.length && <><b>Assets</b><Json value={sel.assets} /></>}
            <b>History</b>
            <ul className="text-xs text-zinc-500">{(sel.history ?? []).map((h: any, i: number) => <li key={i}>{ago(h.ts)} · {h.state ?? h.to} {h.note ?? ""}</li>)}</ul>
          </div>
        )}
      </Drawer>
    </>
  );
}
