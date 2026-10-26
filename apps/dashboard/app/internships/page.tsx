"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge, Button, Card, Copy, Drawer, Empty, ErrorNote, Field, Fit, PageHeader, Skeleton, Table, Tabs } from "@/components/ui";
import { ago, del, fmtTime, post, until, useApi } from "@/lib/api";
import { EVENT_STAGES, STAGE_LABEL, STAGE_TONE, toTs } from "@/lib/stages";

function StageMover({ opp, onDone }: { opp: any; onDone: () => void }) {
  const [stage, setStage] = useState(opp.stage);
  const [when, setWhen] = useState("");
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Field label="Stage">
        <select className="field" value={stage} onChange={(e) => setStage(e.target.value)}>
          {Object.entries(STAGE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </Field>
      {EVENT_STAGES.includes(stage) && (
        <Field label="When (optional)"><input type="datetime-local" className="field" value={when} onChange={(e) => setWhen(e.target.value)} /></Field>
      )}
      <Button variant="primary" disabled={stage === opp.stage && !when}
        onClick={() => post(`/internships/${opp.id}/stage`, { stage, event_at: toTs(when) }).then(onDone)}>Move</Button>
    </div>
  );
}

function Detail({ id, onClose, onChange }: { id: string; onClose: () => void; onChange: () => void }) {
  const { data: o, error, reload } = useApi<any>(`/internships/${id}`);
  const done = () => { reload(); onChange(); };
  const d = o?.application?.drafts;
  return (
    <Drawer open onClose={onClose} title={o?.title ?? "…"}>
      <ErrorNote error={error} />
      {!o ? <Skeleton /> : (
        <div className="space-y-5 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={STAGE_TONE[o.stage]}>{STAGE_LABEL[o.stage]}</Badge>
            <Badge>{o.verification_status}</Badge>
            <Fit score={o.relevance_score} />
            {o.official_url && <a className="text-sky-600 hover:underline" href={o.official_url} target="_blank" rel="noreferrer">Official page ↗</a>}
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <dt className="text-zinc-500">Company</dt><dd>{o.organizer ?? "unknown"}</dd>
            <dt className="text-zinc-500">Location</dt><dd>{o.location ?? o.remote_status ?? "unknown"}</dd>
            <dt className="text-zinc-500">Stipend</dt><dd>{o.stipend ?? "not stated"}</dd>
            <dt className="text-zinc-500">Deadline</dt><dd>{o.deadline ? `${fmtTime(o.deadline)} (in ${until(o.deadline)})` : "not stated"}</dd>
            <dt className="text-zinc-500">Posted</dt><dd>{o.published_at ? ago(o.published_at) : "unknown"}{o.reposts ? ` · reposted ${o.reposts}×` : ""}</dd>
            <dt className="text-zinc-500">Source</dt><dd>{o.discovery_source}</dd>
          </dl>
          {!!o.ai_reasons?.length && <div><b>Why it fits:</b> {o.ai_reasons.join("; ")}</div>}
          {!!o.ai_red_flags?.length && <div className="text-amber-700 dark:text-amber-400"><b>Watch out:</b> {o.ai_red_flags.join("; ")}</div>}
          {(o.ghost_risk ?? 0) >= 0.3 && <div className="rounded-md bg-amber-50 p-2 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
            ⚠️ Ghost-listing risk {Math.round(o.ghost_risk * 100)}%: {(o.ghost_reasons ?? []).join("; ")}</div>}
          {o.summary && <p className="whitespace-pre-line text-zinc-600 dark:text-zinc-400">{o.summary.slice(0, 1200)}</p>}

          <div className="flex flex-wrap gap-2">
            {["found", "shortlisted"].includes(o.stage) && <Button variant="primary" onClick={() => post(`/opportunities/${o.id}/confirm`, {}).then(done)}>Prepare application</Button>}
            <Button onClick={() => post(`/internships/${o.id}/tailor`).then(done)}>Tailor resume</Button>
            {o.organizer && <Button onClick={() => post("/dossier", { company: o.organizer }).then(() => alert("The brief arrives on Telegram in a minute."))}>Company brief</Button>}
            {o.organizer && <Link className="inline-flex items-center rounded-md px-3 py-1.5 text-xs ring-1 ring-zinc-300 dark:ring-zinc-700" href={`/prep?company=${encodeURIComponent(o.organizer)}`}>Referrals</Link>}
            {o.stage !== "skipped" && <Button onClick={() => post(`/opportunities/${o.id}/skip`, { reason: "not for me" }).then(done)}>Skip</Button>}
          </div>
          <StageMover opp={o} onDone={done} />

          {d && (
            <Card title={`Drafts${d.ats?.score != null ? ` · keyword match ${d.ats.score}%` : ""}`}
              action={o.application && <a className="text-xs text-sky-600 hover:underline" href={`/api/resume/pdf?application_id=${o.application.id}`}>One-page PDF ↓</a>}>
              {d.ats?.missing?.length ? <p className="mb-2 text-xs text-zinc-500">Missing keywords: {d.ats.missing.slice(0, 10).join(", ")}</p> : null}
              {d.cover_letter && <div className="mb-3"><div className="mb-1 flex justify-between font-medium">Cover letter <Copy text={d.cover_letter} /></div>
                <p className="whitespace-pre-line text-zinc-600 dark:text-zinc-400">{d.cover_letter}</p></div>}
              {!!d.bullets?.length && <div className="mb-3"><div className="mb-1 flex justify-between font-medium">Resume bullets <Copy text={d.bullets.map((b: any) => `• ${b.text}`).join("\n")} /></div>
                <ul className="list-disc pl-5 text-zinc-600 dark:text-zinc-400">{d.bullets.map((b: any, i: number) => <li key={i}>{b.text}</li>)}</ul></div>}
              {Object.entries(d.answers ?? {}).map(([k, v]: any) => (
                <div key={k} className="mb-2"><div className="flex justify-between font-medium">{k.replaceAll("_", " ")} <Copy text={v} /></div>
                  <p className="whitespace-pre-line text-zinc-600 dark:text-zinc-400">{v}</p></div>
              ))}
              {!!d.missing_facts?.length && <p className="text-xs text-amber-700">Add these to your profile for better drafts: {d.missing_facts.join(", ")}</p>}
            </Card>
          )}
          {!!o.history?.length && (
            <div><div className="mb-1 font-medium">History</div>
              <ul className="space-y-1 text-xs text-zinc-500">{o.history.map((h: any, i: number) => (
                <li key={i}>{fmtTime(h.at)} · {STAGE_LABEL[h.stage] ?? h.stage} · {h.by}{h.note ? ` · ${h.note}` : ""}</li>))}</ul></div>
          )}
        </div>
      )}
    </Drawer>
  );
}

function Listings() {
  const [q, setQ] = useState("");
  const [loc, setLoc] = useState("");
  const [minFit, setMinFit] = useState("0.5");
  const [stage, setStage] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  const params = new URLSearchParams({ q, location: loc, min_fit: minFit, stage }).toString();
  const { data, error, reload } = useApi<any[]>(`/internships?${params}`);
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("id");
    if (id) setSel(id);
  }, []);
  return (
    <>
      <div className="mb-4 grid gap-2 sm:grid-cols-4">
        <input className="field" placeholder="Search: ML, backend, Razorpay…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="field" value={loc} onChange={(e) => setLoc(e.target.value)}>
          <option value="">Anywhere</option><option value="bengaluru">Bengaluru</option><option value="bangalore">Bangalore</option>
          <option value="remote">Remote</option><option value="india">India</option>
        </select>
        <select className="field" value={minFit} onChange={(e) => setMinFit(e.target.value)}>
          <option value="0">Any fit</option><option value="0.5">Fit ≥ 50%</option><option value="0.7">Fit ≥ 70%</option>
        </select>
        <select className="field" value={stage} onChange={(e) => setStage(e.target.value)}>
          <option value="">Any stage</option>{Object.entries(STAGE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </div>
      <ErrorNote error={error} />
      <Card>
        <Table rows={data} onRow={(o) => setSel(o.id)} empty="Nothing matches. Loosen the filters or add company boards." cols={[
          { key: "title", label: "Internship", render: (o) => <div className="max-w-md"><div className="font-medium">{o.title}</div>
            <div className="text-xs text-zinc-500">{[o.organizer, o.location, o.stipend].filter(Boolean).join(" · ")}</div>
            {(o.ghost_risk ?? 0) >= 0.5 && <div className="text-xs text-amber-600">⚠️ possible ghost listing</div>}</div> },
          { key: "fit", label: "Fit", render: (o) => <Fit score={o.relevance_score} /> },
          { key: "deadline", label: "Closes", render: (o) => o.deadline ? <span title={fmtTime(o.deadline)}>{until(o.deadline)}</span> : <span className="text-zinc-400">—</span> },
          { key: "stage", label: "Stage", render: (o) => <Badge tone={STAGE_TONE[o.stage]}>{STAGE_LABEL[o.stage]}</Badge> },
        ]} />
      </Card>
      {sel && <Detail id={sel} onClose={() => setSel(null)} onChange={reload} />}
    </>
  );
}

function Boards() {
  const { data, error, reload } = useApi<any[]>("/boards");
  const [f, setF] = useState({ company: "", ats: "greenhouse", slug: "", domain: "" });
  const [test, setTest] = useState<any>(null);
  const [probe, setProbe] = useState<any>(null);
  const body = { ...f, domain: f.domain || null };
  return (
    <>
      <ErrorNote error={error} />
      <Card title="Add a company's careers board" className="mb-6">
        <p className="mb-3 text-xs text-zinc-500">Official Greenhouse, Lever and Ashby job-board APIs only. The slug is the company part of the board URL (boards.greenhouse.io/<b>slug</b>, jobs.lever.co/<b>slug</b>, jobs.ashbyhq.com/<b>slug</b>). Test it first: the board is added only if it answers.</p>
        <div className="grid gap-2 sm:grid-cols-5">
          <input className="field" placeholder="Company" value={f.company} onChange={(e) => setF({ ...f, company: e.target.value })} />
          <select className="field" value={f.ats} onChange={(e) => setF({ ...f, ats: e.target.value })}>
            <option>greenhouse</option><option>lever</option><option>ashby</option></select>
          <input className="field" placeholder="slug" value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value })} />
          <input className="field" placeholder="domain (optional)" value={f.domain} onChange={(e) => setF({ ...f, domain: e.target.value })} />
          <div className="flex gap-2">
            <Button disabled={!f.company || !f.slug} onClick={async () => setTest(await post("/boards/test", body))}>Test</Button>
            <Button variant="primary" disabled={!test?.ok} onClick={async () => { await post("/boards", body); setTest(null); setF({ company: "", ats: "greenhouse", slug: "", domain: "" }); reload(); }}>Add</Button>
          </div>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <Button disabled={!f.company} onClick={async () => setProbe(await post("/boards/probe", { company: f.company, domain: f.domain || null }))}>Don't know the slug? Find it</Button>
          {probe && (probe.found.length ? probe.found.map((p: any) => (
            <button key={p.ats + p.slug} className="text-xs text-sky-600 hover:underline" onClick={() => { setF({ ...f, ats: p.ats, slug: p.slug }); setTest(p); }}>
              {p.ats}/{p.slug} ({p.items} BLR/India intern roles)</button>)) : <span className="text-xs text-zinc-500">No board found under common spellings.</span>)}
        </div>
        {test && (
          <div className={`mt-3 rounded-md p-2 text-xs ${test.ok ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" : "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300"}`}>
            {test.ok ? <>Works: {test.total} open roles, {test.items} match Bengaluru / India / remote internships.
              {test.sample?.length ? <ul className="mt-1 list-disc pl-4">{test.sample.map((s: any, i: number) => <li key={i}>{s.title} · {s.location}</li>)}</ul> : null}</>
              : <>Didn't answer: {test.error}</>}
          </div>
        )}
      </Card>
      <Card title="Your company boards">
        <Table rows={data} empty="No boards yet." cols={[
          { key: "company", label: "Company", render: (b) => <><div className="font-medium">{b.company}</div><div className="text-xs text-zinc-500">{b.ats}/{b.slug}</div></> },
          { key: "found", label: "Found" },
          { key: "last_success", label: "Last checked", render: (b) => b.last_error ? <span className="text-xs text-rose-600" title={b.last_error}>error</span> : ago(b.last_success) },
          { key: "enabled", label: "", render: (b) => b.enabled ? <Button onClick={() => del(`/boards/${b.id}`).then(reload)}>Remove</Button> : <Badge>off</Badge> },
        ]} />
      </Card>
    </>
  );
}

function Drives() {
  const { data } = useApi<any[]>("/drives", { live: false });
  return (
    <Card title="Off-campus drives" action={<span className="text-xs text-zinc-500">Months are approximate; always check the page</span>}>
      {!data ? <Skeleton /> : !data.length ? <Empty>No drives in config/drives.yaml.</Empty> : (
        <ul className="divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
          {data.map((d) => (
            <li key={d.id} className="py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{d.name}</span>
                {d.phase === "open" && <Badge tone="green">usually open now</Badge>}
                {d.phase === "soon" && <Badge tone="amber">opens next month</Badge>}
                <span className="text-xs text-zinc-500">usually {d.window}</span>
              </div>
              <div className="text-xs text-zinc-500">{d.eligibility}</div>
              <a className="text-xs text-sky-600 hover:underline" href={d.url} target="_blank" rel="noreferrer">{d.url}</a>
              {d.page_changed_at && <span className="ml-2 text-xs text-amber-600">page changed {ago(d.page_changed_at)}</span>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function Internships() {
  const [tab, setTab] = useState("listings");
  return (
    <>
      <PageHeader title="Internships" subtitle="Bengaluru, India and remote internships from company boards, SimplifyJobs, news and links you add. Fit = 40% rules + 60% AI." />
      <Tabs tabs={["listings", "company_boards", "drives"]} value={tab} onChange={setTab} />
      {tab === "listings" && <Listings />}
      {tab === "company_boards" && <Boards />}
      {tab === "drives" && <Drives />}
    </>
  );
}

