"use client";
import { useState } from "react";
import { Badge, Button, Card, ErrorNote, PageHeader, Table } from "@/components/ui";
import { ago, post, useApi } from "@/lib/api";

export default function Knowledge() {
  const { data, error, reload } = useApi<any[]>("/knowledge");
  const [url, setUrl] = useState("");
  return (
    <>
      <PageHeader title="Knowledge" subtitle="Articles and links captured into your NOESIS vault. Social video links go to 01 Inbox for your local pipeline." />
      <ErrorNote error={error} />
      <Card className="mb-6">
        <form className="flex gap-2" onSubmit={async (e) => { e.preventDefault(); await post("/knowledge/ingest", { url }); setUrl(""); reload(); }}>
          <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…"
            className="flex-1 rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700" />
          <Button type="submit" variant="primary">Capture</Button>
        </form>
      </Card>
      <Card>
        <Table rows={data} cols={[
          { key: "title", label: "Title", render: (k) => <div className="max-w-xl"><div className="font-medium">{k.title ?? k.url}</div><div className="line-clamp-2 text-xs text-zinc-500">{k.summary}</div></div> },
          { key: "status", label: "Status", render: (k) => <Badge>{k.status}</Badge> },
          { key: "note_path", label: "Vault note", render: (k) => <span className="text-xs text-zinc-500">{k.note_path}</span> },
          { key: "created_at", label: "Captured", render: (k) => ago(k.created_at) },
        ]} />
      </Card>
    </>
  );
}
