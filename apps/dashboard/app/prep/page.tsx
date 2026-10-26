"use client";
import { useEffect, useState } from "react";
import { Badge, Button, Card, Copy, Empty, ErrorNote, Field, PageHeader, Skeleton, Table, Tabs } from "@/components/ui";
import { ago, api, post, useApi } from "@/lib/api";

function Referrals({ initial }: { initial: string }) {
  const [q, setQ] = useState(initial);
  const [plan, setPlan] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const { data, reload } = useApi<any>("/referrals");
  const [f, setF] = useState({ company: initial, name: "", profile_url: "", relation: "alumni" });
  const find = async (company: string) => {
    setErr(null);
    try { setPlan((await api(`/referrals?q=${encodeURIComponent(company)}`)).plan); } catch (x: any) { setErr(x.message); }
  };
  useEffect(() => { if (initial) find(initial); }, [initial]);
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Who to ask">
        <form className="mb-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); find(q); }}>
          <input className="field" placeholder="Company, e.g. Razorpay" value={q} onChange={(e) => setQ(e.target.value)} />
          <Button type="submit" variant="primary" disabled={!q}>Find</Button>
        </form>
        <ErrorNote error={err} />
        {plan && (
          <div className="space-y-3 text-sm">
            <p className="text-xs text-zinc-500">These open LinkedIn / Google in your browser. NOESIS never opens LinkedIn itself and never messages anyone.</p>
            <ul className="space-y-1">{plan.links.map((l: any) => <li key={l.url}><a className="text-sky-600 hover:underline" href={l.url} target="_blank" rel="noreferrer">{l.label} ↗</a></li>)}</ul>
            {!plan.college_known && <p className="text-xs text-amber-700">Add your college to your profile for an alumni search.</p>}
            <div className="rounded-lg bg-zinc-50 p-3 dark:bg-zinc-950">
              <div className="mb-1 flex justify-between text-xs font-medium">A note you can send (edit it first) <Copy text={plan.note} /></div>
              <p>{plan.note}</p>
            </div>
          </div>
        )}
      </Card>
      <Card title="People you're asking">
        <Table rows={data?.list} empty="Nobody yet. Add the people you message." cols={[
          { key: "name", label: "Person", render: (r) => <>{r.profile_url ? <a className="font-medium hover:underline" href={r.profile_url} target="_blank" rel="noreferrer">{r.name}</a> : <span className="font-medium">{r.name}</span>}
            <div className="text-xs text-zinc-500">{r.company} · {r.relation}</div></> },
          { key: "status", label: "Status", render: (r) => (
            <select className="rounded border border-zinc-200 bg-transparent px-1 py-0.5 text-xs dark:border-zinc-700" value={r.status}
              onChange={(e) => post(`/referrals/${r.id}/status`, { status: e.target.value }).then(reload)}>
              {["to_contact", "messaged", "replied", "referred", "declined"].map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}
            </select>) },
          { key: "updated_at", label: "Updated", render: (r) => ago(r.updated_at) },
        ]} />
        <form className="mt-3 grid gap-2 sm:grid-cols-2" onSubmit={async (e) => {
          e.preventDefault(); await post("/referrals", { ...f, profile_url: f.profile_url || null }); setF({ ...f, name: "", profile_url: "" }); reload();
        }}>
          <input className="field" placeholder="Company" value={f.company} onChange={(e) => setF({ ...f, company: e.target.value })} />
          <input className="field" placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          <input className="field" placeholder="Profile link (optional)" value={f.profile_url} onChange={(e) => setF({ ...f, profile_url: e.target.value })} />
          <div className="flex gap-2">
            <select className="field" value={f.relation} onChange={(e) => setF({ ...f, relation: e.target.value })}>
              <option>alumni</option><option>employee</option><option>friend</option><option>recruiter</option></select>
            <Button type="submit" variant="primary" disabled={!f.company || !f.name}>Add</Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

function Questions() {
  const [company, setCompany] = useState("");
  const { data, reload } = useApi<any>(`/questions?company=${encodeURIComponent(company)}`);
  const [f, setF] = useState({ company: "", stage: "interview_1", text: "", went_well: "" });
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Log a debrief" className="lg:col-span-1">
        <form className="space-y-2" onSubmit={async (e) => {
          e.preventDefault();
          const questions = f.text.split(/\n|;/).map((s) => s.trim()).filter((s) => s.length > 5);
          await post("/debrief", { company: f.company, stage: f.stage, questions, went_well: f.went_well === "" ? null : f.went_well === "yes" });
          setF({ ...f, text: "" }); reload();
        }}>
          <input className="field" placeholder="Company" value={f.company} onChange={(e) => setF({ ...f, company: e.target.value })} />
          <select className="field" value={f.stage} onChange={(e) => setF({ ...f, stage: e.target.value })}>
            <option value="online_assessment">Online assessment</option><option value="interview_1">Interview 1</option>
            <option value="interview_2">Interview 2</option><option value="hr">HR round</option></select>
          <Field label="What they asked" hint="One per line."><textarea className="field h-32" value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} /></Field>
          <select className="field" value={f.went_well} onChange={(e) => setF({ ...f, went_well: e.target.value })}>
            <option value="">How did it go?</option><option value="yes">Went well</option><option value="no">Struggled</option></select>
          <Button type="submit" variant="primary" disabled={!f.company || f.text.length < 6}>Save</Button>
        </form>
      </Card>
      <Card title="Your question bank" className="lg:col-span-2"
        action={<select className="rounded border border-zinc-200 bg-transparent px-1 py-0.5 text-xs dark:border-zinc-700" value={company} onChange={(e) => setCompany(e.target.value)}>
          <option value="">All companies</option>{(data?.by_company ?? []).map((c: any) => <option key={c.company} value={c.company}>{c.company} ({c.n})</option>)}</select>}>
        {!data ? <Skeleton /> : !data.questions.length ? <Empty>Nothing yet. Your Interview Coach agent drills you on these.</Empty> : (
          <ul className="divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
            {data.questions.map((q: any) => (
              <li key={q.id} className="py-2">{q.question}
                <div className="mt-0.5 flex gap-1 text-xs"><Badge tone="blue">{q.topic}</Badge><Badge>{q.company}</Badge>{q.stage && <Badge tone="gray">{q.stage}</Badge>}
                  {q.went_well === 0 && <Badge tone="amber">practise again</Badge>}</div></li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function Dossier({ initial }: { initial: string }) {
  const [company, setCompany] = useState(initial);
  const [d, setD] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = async (c: string) => setD(c ? await api(`/dossier?company=${encodeURIComponent(c)}`) : null);
  useEffect(() => { if (initial) load(initial); }, [initial]);
  return (
    <Card title="Company brief">
      <div className="mb-3 flex flex-wrap gap-2">
        <input className="field max-w-xs" placeholder="Company" value={company} onChange={(e) => setCompany(e.target.value)} />
        <Button onClick={() => load(company)} disabled={!company}>Show saved</Button>
        <Button variant="primary" disabled={!company} onClick={async () => { await post("/dossier", { company, refresh: true }); setMsg("Building it: it arrives on Telegram and here in a minute."); }}>Build fresh</Button>
      </div>
      {msg && <p className="mb-2 text-xs text-zinc-500">{msg}</p>}
      {d ? <pre className="whitespace-pre-wrap font-sans text-sm">{d.text}</pre> : <Empty>A brief is built automatically when a listing moves to an OA or interview. Build one any time here.</Empty>}
    </Card>
  );
}

export default function Prep() {
  const [tab, setTab] = useState("referrals");
  const [company, setCompany] = useState("");
  useEffect(() => {
    const c = new URLSearchParams(window.location.search).get("company");
    if (c) setCompany(c);
  }, []);
  return (
    <>
      <PageHeader title="Prep & people" subtitle="Referrals you ask for yourself, the questions you've been asked, and company briefs before a round." />
      <Tabs tabs={["referrals", "question_bank", "company_brief"]} value={tab} onChange={setTab} />
      {tab === "referrals" && <Referrals initial={company} />}
      {tab === "question_bank" && <Questions />}
      {tab === "company_brief" && <Dossier initial={company} />}
    </>
  );
}
