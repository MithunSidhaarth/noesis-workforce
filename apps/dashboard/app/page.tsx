"use client";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, Fit, PageHeader, Skeleton, Stat } from "@/components/ui";
import { fmtTime, post, until, useApi } from "@/lib/api";
import { STAGE_LABEL } from "@/lib/stages";

function AddLink({ onDone }: { onDone: () => void }) {
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const linkedin = /linkedin\.com/i.test(url);
  return (
    <form className="space-y-2" onSubmit={async (e) => {
      e.preventDefault();
      try {
        await post("/internships/link", { url, text });
        setMsg("Added. I'm reading it now; it shows up under Internships in a minute.");
        setUrl(""); setText(""); onDone();
      } catch (err: any) { setMsg(err.message); }
    }}>
      <input className="field" placeholder="Paste a posting link (Internshala, Unstop, Wellfound, a careers page…)" value={url} onChange={(e) => setUrl(e.target.value)} />
      {(linkedin || text) && (
        <textarea className="field h-24" placeholder="LinkedIn pages are never opened: paste the job description here" value={text} onChange={(e) => setText(e.target.value)} />
      )}
      <div className="flex items-center gap-3">
        <Button type="submit" variant="primary" disabled={!url}>Add</Button>
        {msg && <span className="text-xs text-zinc-500">{msg}</span>}
      </div>
    </form>
  );
}

export default function Home() {
  const { data, error, reload } = useApi<any>("/home");
  const s = data?.streak;
  const doneToday = s && s.today >= s.quota;
  return (
    <>
      <PageHeader title="Today" subtitle="Your applications for today, what needs a tap, and what's coming up." />
      <ErrorNote error={error} />
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Applied today" value={s ? `${s.today}/${s.quota}` : "—"} hint={doneToday ? "Quota done 🎉" : "daily quota"} tone={s && !doneToday ? "amber" : undefined} />
        <Stat label="Streak" value={s ? `${s.streak}d` : "—"} hint={s ? `best ${s.best}d` : undefined} />
        <Stat label="New listings (24h)" value={data?.new_24h ?? "—"} />
        <Stat label="Interviews this week" value={data ? data.upcoming.filter((u: any) => u.next_event_at - Date.now() / 1000 < 7 * 86400).length : "—"} />
      </div>

      {!!data?.proposals?.length && (
        <Card title="Needs your tap" className="mb-6">
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {data.proposals.map((p: any) => (
              <li key={p.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{p.title}{p.organizer ? ` · ${p.organizer}` : ""}</div>
                  <div className="text-xs text-zinc-500">Move to {STAGE_LABEL[p.stage]}? “{p.evidence}”</div>
                </div>
                <Button variant="primary" onClick={() => post(`/proposals/${p.id}`, { accept: true }).then(reload)}>Move</Button>
                <Button onClick={() => post(`/proposals/${p.id}`, { accept: false }).then(reload)}>Not this</Button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title={`Apply today${s ? ` (${s.quota})` : ""}`} className="lg:col-span-2"
          action={<Link href="/internships" className="text-xs text-sky-600 hover:underline">All internships →</Link>}>
          {!data ? <Skeleton /> : !data.today.length ? <Empty>No strong open matches right now. The Scout checks your sources every 15 minutes.</Empty> : (
            <ul className="space-y-3">
              {data.today.map((o: any) => (
                <li key={o.id} className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <Link href={`/internships?id=${o.id}`} className="font-medium hover:underline">{o.title}</Link>
                      <div className="text-xs text-zinc-500">{[o.organizer, o.location, o.stipend].filter(Boolean).join(" · ")}</div>
                    </div>
                    <Fit score={o.relevance_score} />
                  </div>
                  {!!o.ai_reasons?.length && <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">Why: {o.ai_reasons.join("; ")}</p>}
                  {(o.ghost_risk ?? 0) >= 0.5 && <p className="mt-1 text-xs text-amber-600">⚠️ Possible ghost listing</p>}
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span className="text-xs text-zinc-500">{o.deadline ? `closes in ${until(o.deadline)}` : "no deadline listed"}</span>
                    <span className="flex-1" />
                    <Button onClick={() => post(`/internships/${o.id}/tailor`).then(reload)}>Tailor resume</Button>
                    {o.stage !== "preparing" && <Button variant="primary" onClick={() => post(`/opportunities/${o.id}/confirm`, {}).then(reload)}>Prepare</Button>}
                    {o.official_url && <a href={o.official_url} target="_blank" rel="noreferrer" className="text-xs text-sky-600 hover:underline">Open ↗</a>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="Coming up">
            {!data ? <Skeleton /> : !data.upcoming.length && !data.deadlines.length ? <Empty>Nothing scheduled.</Empty> : (
              <ul className="space-y-2 text-sm">
                {data.upcoming.map((u: any) => (
                  <li key={`e${u.id}`}><Badge tone="amber">{STAGE_LABEL[u.stage]}</Badge> <span className="font-medium">{u.organizer ?? u.title}</span>
                    <div className="text-xs text-zinc-500">{fmtTime(u.next_event_at)}</div></li>
                ))}
                {data.deadlines.map((u: any) => (
                  <li key={`d${u.id}`}><Badge tone="blue">deadline</Badge> <span className="font-medium">{u.title}</span>
                    <div className="text-xs text-zinc-500">{fmtTime(u.deadline)} · in {until(u.deadline)}</div></li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="This week">
            {data ? (
              <div className="grid grid-cols-3 gap-2 text-center text-sm">
                {[["applied", "Applied"], ["online_assessment", "OAs"], ["interview", "Interviews"]].map(([k, l]) => (
                  <div key={k}><div className="text-xl font-semibold tabular-nums">{data.funnel_7d[k]}</div><div className="text-xs text-zinc-500">{l}</div></div>
                ))}
              </div>
            ) : <Skeleton />}
          </Card>
          <Card title="Found one yourself?"><AddLink onDone={reload} /></Card>
        </div>
      </div>
    </>
  );
}
