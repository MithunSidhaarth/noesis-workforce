"use client";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Card, ErrorNote, PageHeader, Stat, Table } from "@/components/ui";
import { ago, fmtTime, post, until, useApi } from "@/lib/api";

export default function Overview() {
  const { data, error, reload } = useApi<any>("/overview");
  const [text, setText] = useState("");
  const q = data?.queue ?? {};
  const t24 = data?.tasks_24h ?? {};
  return (
    <>
      <PageHeader title="Overview" subtitle="What your workforce is doing right now. Everything here is read from the live database." />
      <ErrorNote error={error} />
      <Card className="mb-6">
        <form className="flex flex-col gap-2 sm:flex-row" onSubmit={async (e) => {
          e.preventDefault();
          if (text.trim().length < 3) return;
          await post("/objective", { text });
          setText("");
          reload();
        }}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Give the Chief of Staff an objective, e.g. “Find AI hackathons I can join this month”"
            className="flex-1 rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm dark:border-zinc-700" />
          <Button type="submit" variant="primary">Send objective</Button>
        </form>
      </Card>
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Queued" value={(q.queued ?? 0) + (q.pending ?? 0)} hint={`${q.running ?? 0} running`} />
        <Stat label="Waiting approval" value={data?.pending_approvals ?? "…"} tone={data?.pending_approvals ? "amber" : undefined} />
        <Stat label="Succeeded 24h" value={t24.succeeded ?? 0} />
        <Stat label="Failed 24h" value={(t24.failed ?? 0) + (t24.dead ?? 0)} tone={(t24.dead ?? 0) > 0 ? "red" : undefined} hint={`${t24.dead ?? 0} dead-lettered`} />
        <Stat label="New opportunities" value={data?.opportunities_24h ?? "…"} hint="last 24h" />
        <Stat label="Model spend today" value={`$${(data?.spend_today_usd ?? 0).toFixed(3)}`} />
      </div>
      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Bots" className="xl:col-span-2" action={<Link href="/bots" className="text-xs text-zinc-500 hover:underline">All bots →</Link>}>
          <Table rows={data?.bots} cols={[
            { key: "name", label: "Bot", render: (b) => <Link className="font-medium hover:underline" href={`/bots/view?id=${b.id}`}>{b.name}</Link> },
            { key: "state", label: "State", render: (b) => <Badge>{b.enabled ? b.state : "disabled"}</Badge> },
            { key: "last_heartbeat", label: "Heartbeat", render: (b) => ago(b.last_heartbeat) },
            { key: "last_success", label: "Last success", render: (b) => ago(b.last_success) },
            { key: "n", label: "OK / failed", className: "tabular-nums", render: (b) => `${b.tasks_succeeded} / ${b.tasks_failed}` },
          ]} />
        </Card>
        <div className="space-y-6">
          <Card title="Upcoming deadlines">
            {data?.upcoming_deadlines?.length ? (
              <ul className="space-y-2 text-sm">
                {data.upcoming_deadlines.map((o: any) => (
                  <li key={o.id} className="flex items-start justify-between gap-2">
                    <span className="line-clamp-2">{o.title}</span>
                    <span className="shrink-0 text-xs text-zinc-500" title={fmtTime(o.deadline)}>{until(o.deadline)}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-zinc-500">No verified upcoming deadlines.</p>}
          </Card>
          <Card title="Integrations" action={<Link href="/integrations" className="text-xs text-zinc-500 hover:underline">Details →</Link>}>
            <ul className="space-y-1.5 text-sm">
              {data?.integrations?.map((i: any) => (
                <li key={i.name} className="flex items-center justify-between"><span className="capitalize">{i.name}</span><Badge>{i.status}</Badge></li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
      <Card title="Recent activity" className="mt-6" action={<Link href="/logs" className="text-xs text-zinc-500 hover:underline">Logs →</Link>}>
        <ul className="divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
          {data?.recent_activity?.map((l: any, i: number) => (
            <li key={i} className="flex gap-3 py-1.5">
              <span className="w-20 shrink-0 text-xs text-zinc-400">{ago(l.ts)}</span>
              <span className="w-32 shrink-0 truncate text-xs text-zinc-500">{l.bot_id ?? "system"}</span>
              <span className={l.level === "error" ? "text-rose-600" : l.level === "warning" ? "text-amber-700 dark:text-amber-400" : ""}>{l.message}</span>
            </li>
          ))}
        </ul>
      </Card>
    </>
  );
}
