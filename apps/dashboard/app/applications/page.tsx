"use client";
import Link from "next/link";
import { useState } from "react";
import Submissions from "@/components/Submissions";
import { Button, Card, Empty, ErrorNote, Field, Fit, Modal, PageHeader, Skeleton, Table, Tabs } from "@/components/ui";
import { del, post, until, useApi } from "@/lib/api";
import { EVENT_STAGES, STAGE_LABEL, inr, toTs } from "@/lib/stages";

type Move = { opp: any; stage: string } | null;

function Board() {
  const { data, error, reload } = useApi<any>("/pipeline");
  const [drag, setDrag] = useState<any>(null);
  const [over, setOver] = useState<string | null>(null);
  const [pending, setPending] = useState<Move>(null);
  const [when, setWhen] = useState("");
  const move = async (opp: any, stage: string, event_at?: number) => {
    await post(`/internships/${opp.id}/stage`, { stage, event_at });
    setPending(null); setWhen(""); reload();
  };
  // an OA / interview asks for its time (for reminders, prep and the calendar); everything else moves at once
  const drop = (opp: any, stage: string) => (opp.stage === stage ? null : EVENT_STAGES.includes(stage) ? setPending({ opp, stage }) : move(opp, stage));
  return (
    <>
      <ErrorNote error={error} />
      {!data ? <Skeleton /> : (
        <div className="-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-4">
          {data.columns.map((c: any) => (
            <div key={c.stage}
              onDragOver={(e) => { e.preventDefault(); setOver(c.stage); }} onDragLeave={() => setOver(null)}
              onDrop={() => { setOver(null); if (drag) drop(drag, c.stage); setDrag(null); }}
              className={`w-64 shrink-0 snap-start rounded-xl border p-2 ${over === c.stage ? "border-sky-400 bg-sky-50 dark:bg-sky-950" : "border-zinc-200 bg-zinc-100/60 dark:border-zinc-800 dark:bg-zinc-900/60"}`}>
              <div className="mb-2 flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                {c.label}<span className="rounded-full bg-white px-1.5 text-zinc-600 dark:bg-zinc-800">{c.items.length}</span>
              </div>
              <div className="space-y-2">
                {c.items.map((o: any) => (
                  <div key={o.id} draggable onDragStart={() => setDrag(o)}
                    className="cursor-grab rounded-lg border border-zinc-200 bg-white p-2 text-sm shadow-sm active:cursor-grabbing dark:border-zinc-700 dark:bg-zinc-900">
                    <Link href={`/internships?id=${o.id}`} className="font-medium hover:underline">{o.title}</Link>
                    <div className="text-xs text-zinc-500">{o.organizer ?? ""}</div>
                    <div className="mt-1 flex items-center justify-between gap-2">
                      <Fit score={o.relevance_score} />
                      <span className="text-[11px] text-zinc-500">
                        {o.next_event_at ? `event in ${until(o.next_event_at)}` : o.deadline && ["shortlisted", "preparing"].includes(o.stage) ? `closes ${until(o.deadline)}` : ""}
                      </span>
                    </div>
                    {/* touch screens can't drag: a select does the same */}
                    <select aria-label="Move to" className="mt-2 w-full rounded border border-zinc-200 bg-transparent px-1 py-0.5 text-xs dark:border-zinc-700 lg:hidden"
                      value="" onChange={(e) => e.target.value && drop(o, e.target.value)}>
                      <option value="">Move to…</option>
                      {data.columns.filter((x: any) => x.stage !== c.stage).map((x: any) => <option key={x.stage} value={x.stage}>{x.label}</option>)}
                    </select>
                  </div>
                ))}
                {!c.items.length && <div className="rounded-lg border border-dashed border-zinc-300 p-3 text-center text-xs text-zinc-400 dark:border-zinc-700">Drop here</div>}
              </div>
            </div>
          ))}
        </div>
      )}
      <Modal open={!!pending} onClose={() => setPending(null)} title={pending ? `${STAGE_LABEL[pending.stage]}: when is it?` : ""}>
        {pending && (
          <div className="space-y-3 text-sm">
            <p className="text-zinc-500">With a time I set reminders a day and an hour before, build a prep plan, and (if enabled) put it on your calendar and call you an hour before.</p>
            <Field label="Date and time"><input type="datetime-local" className="field" value={when} onChange={(e) => setWhen(e.target.value)} /></Field>
            <div className="flex gap-2">
              <Button variant="primary" disabled={!when} onClick={() => move(pending.opp, pending.stage, toTs(when))}>Move with time</Button>
              <Button onClick={() => move(pending.opp, pending.stage)}>Move without a time</Button>
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}

function Offers() {
  const { data, error, reload } = useApi<any>("/offers");
  const blank = { company: "", stipend_month_inr: "", duration_months: "", location: "", mode: "hybrid", ppo: "unknown", learning: "3" };
  const [f, setF] = useState<any>(blank);
  const num = (v: string) => (v === "" ? null : Number(v));
  return (
    <>
      <ErrorNote error={error} />
      <Card title="Compare offers" className="mb-6">
        <Table rows={data?.offers} empty="No offers yet. Add one below when it comes in." cols={[
          { key: "company", label: "Company", render: (o) => <span className="font-medium">{o.company}</span> },
          { key: "score", label: "Score", render: (o) => <span className="font-semibold tabular-nums">{o.score}</span> },
          { key: "stipend", label: "Stipend / mo", render: (o) => inr(o.stipend_month_inr) },
          { key: "duration_months", label: "Months" }, { key: "location", label: "Where", render: (o) => `${o.location ?? "—"} · ${o.mode ?? "?"}` },
          { key: "ppo", label: "PPO" }, { key: "learning", label: "Learning", render: (o) => o.learning ? `${o.learning}/5` : "—" },
          { key: "x", label: "", render: (o) => <Button onClick={() => del(`/offers/${o.id}`).then(reload)}>Remove</Button> },
        ]} />
        {data?.weights && <p className="mt-3 text-xs text-zinc-500">Weights: {Object.entries(data.weights).map(([k, v]: any) => `${k} ${Math.round(v * 100)}%`).join(", ")}. Change them with offer_weights in your profile.</p>}
      </Card>
      <Card title="Add an offer">
        <div className="grid gap-2 sm:grid-cols-4">
          <input className="field" placeholder="Company" value={f.company} onChange={(e) => setF({ ...f, company: e.target.value })} />
          <input className="field" placeholder="Stipend ₹/month" inputMode="numeric" value={f.stipend_month_inr} onChange={(e) => setF({ ...f, stipend_month_inr: e.target.value })} />
          <input className="field" placeholder="Months" inputMode="decimal" value={f.duration_months} onChange={(e) => setF({ ...f, duration_months: e.target.value })} />
          <input className="field" placeholder="City" value={f.location} onChange={(e) => setF({ ...f, location: e.target.value })} />
          <select className="field" value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}><option>onsite</option><option>hybrid</option><option>remote</option></select>
          <select className="field" value={f.ppo} onChange={(e) => setF({ ...f, ppo: e.target.value })}>
            <option value="yes">PPO: yes</option><option value="likely">PPO: likely</option><option value="unknown">PPO: unknown</option><option value="no">PPO: no</option></select>
          <select className="field" value={f.learning} onChange={(e) => setF({ ...f, learning: e.target.value })}>
            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>Learning {n}/5</option>)}</select>
          <Button variant="primary" disabled={!f.company} onClick={async () => {
            await post("/offers", { ...f, stipend_month_inr: num(f.stipend_month_inr), duration_months: num(f.duration_months), learning: num(f.learning), location: f.location || null });
            setF(blank); reload();
          }}>Add offer</Button>
        </div>
      </Card>
    </>
  );
}

function Insights() {
  const { data, error } = useApi<any>("/insights", { live: false });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Skeleton />;
  const steps = [["found", "Found"], ["applied", "Applied"], ["online_assessment", "OA"], ["interview", "Interview"], ["offer", "Offer"]];
  const top = Math.max(1, data.funnel.found);
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Funnel (all time)">
        <div className="space-y-2">
          {steps.map(([k, l]) => (
            <div key={k} className="flex items-center gap-3 text-sm">
              <span className="w-20 text-zinc-500">{l}</span>
              <span className="h-3 flex-1 overflow-hidden rounded bg-zinc-100 dark:bg-zinc-800">
                <span className="block h-full bg-sky-500" style={{ width: `${(100 * data.funnel[k]) / top}%` }} /></span>
              <span className="w-10 text-right tabular-nums">{data.funnel[k]}</span>
            </div>
          ))}
        </div>
      </Card>
      <Card title="What's working">
        {data.insights.length ? <ul className="list-disc space-y-1 pl-5 text-sm">{data.insights.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul>
          : <Empty>Apply to a few more and this fills in.</Empty>}
      </Card>
      <Card title="Response rate by source" className="lg:col-span-2">
        <Table rows={data.by_source} rowKey="name" empty="No applications yet." cols={[
          { key: "name", label: "Source" }, { key: "applied", label: "Applied" }, { key: "replied", label: "Heard back" },
          { key: "rate", label: "Rate", render: (r) => r.rate != null ? `${Math.round(r.rate * 100)}%` : "—" },
        ]} />
      </Card>
    </div>
  );
}

export default function Applications() {
  const [tab, setTab] = useState("board");
  return (
    <>
      <PageHeader title="Applications" subtitle="Every listing you're working on, from shortlisted to offer. Drag a card to change its stage." />
      <Tabs tabs={["board", "submissions", "offers", "insights"]} value={tab} onChange={setTab} />
      {tab === "board" && <Board />}
      {tab === "submissions" && <Submissions />}
      {tab === "offers" && <Offers />}
      {tab === "insights" && <Insights />}
    </>
  );
}
