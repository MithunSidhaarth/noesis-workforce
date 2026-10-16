"use client";
import Link from "next/link";
import { Badge, Button, Card, ErrorNote, PageHeader, Table } from "@/components/ui";
import { ago, post, useApi } from "@/lib/api";

export default function Bots() {
  const { data, error, reload } = useApi<any[]>("/bots");
  const act = (id: string, op: string) => post(`/bots/${id}/${op}`).then(reload);
  return (
    <>
      <PageHeader title="Bots" subtitle="Each bot has its own configuration, permissions, memory scope and schedules. Bots cannot change their own permissions." />
      <ErrorNote error={error} />
      <Card>
        <Table rows={data} cols={[
          { key: "name", label: "Bot", render: (b) => (
            <div><Link href={`/bots/view?id=${b.id}`} className="font-medium hover:underline">{b.name}</Link>
              <div className="max-w-md text-xs text-zinc-500">{b.purpose}</div></div>) },
          { key: "state", label: "State", render: (b) => <Badge>{b.enabled ? b.state : "disabled"}</Badge> },
          { key: "tools", label: "Tools", render: (b) => <span className="text-xs text-zinc-500">{(b.config?.tools ?? []).join(", ") || "none"}</span> },
          { key: "hb", label: "Heartbeat", render: (b) => ago(b.last_heartbeat) },
          { key: "ops", label: "", render: (b) => (
            <div className="flex gap-1">
              {b.state === "paused" ? <Button onClick={() => act(b.id, "resume")}>Resume</Button> : <Button onClick={() => act(b.id, "pause")}>Pause</Button>}
              {b.enabled ? <Button variant="danger" onClick={() => act(b.id, "disable")}>Disable</Button> : <Button onClick={() => act(b.id, "enable")}>Enable</Button>}
            </div>) },
        ]} />
      </Card>
    </>
  );
}
