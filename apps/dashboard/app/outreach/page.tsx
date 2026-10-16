"use client";
import { useState } from "react";
import { Badge, Button, Card, ErrorNote, PageHeader, Table, Tabs } from "@/components/ui";
import { ago, post, useApi } from "@/lib/api";

export default function Outreach() {
  const { data, error, reload } = useApi<any>("/outreach");
  const [tab, setTab] = useState("contacts");
  const [form, setForm] = useState({ email: "", name: "", organization: "", role: "", source_url: "", notes: "" });
  return (
    <>
      <PageHeader title="Outreach" subtitle="Honest, low-volume, personalised outreach. Opt-outs are permanent. Sending is off until you enable it." />
      <ErrorNote error={error} />
      <Tabs tabs={["contacts", "messages", "suppressed", "add_contact"]} value={tab} onChange={setTab} />
      {tab === "contacts" && <Card><Table rows={data?.contacts} cols={[
        { key: "name", label: "Contact", render: (c) => <div><div className="font-medium">{c.name ?? c.email}</div><div className="text-xs text-zinc-500">{c.role} {c.organization && `· ${c.organization}`}</div></div> },
        { key: "status", label: "Status", render: (c) => <Badge>{c.status}</Badge> },
        { key: "relevance", label: "Relevance", render: (c) => c.relevance?.toFixed(2) ?? "—" },
        { key: "last_contacted", label: "Last contact", render: (c) => ago(c.last_contacted) },
        { key: "ops", label: "", render: (c) => c.status !== "opted_out" && <Button onClick={() => post("/outreach/opt-out", { email: c.email }).then(reload)}>Mark opted out</Button> },
      ]} /></Card>}
      {tab === "messages" && <Card><Table rows={data?.messages} cols={[
        { key: "subject", label: "Subject", render: (m) => <div className="max-w-lg"><div className="font-medium">{m.subject}</div><div className="line-clamp-2 text-xs text-zinc-500">{m.body}</div></div> },
        { key: "kind", label: "Kind" }, { key: "status", label: "Status", render: (m) => <Badge>{m.status}</Badge> },
        { key: "ops", label: "", render: (m) => m.status === "draft" && <Button onClick={() => post(`/outreach/messages/${m.id}/send`).then(reload)}>Request send</Button> },
      ]} /></Card>}
      {tab === "suppressed" && <Card><Table rows={data?.suppressed} rowKey="email" empty="Nobody has opted out." cols={[
        { key: "email", label: "Email" }, { key: "reason", label: "Reason" }, { key: "created_at", label: "Since", render: (s) => ago(s.created_at) },
      ]} /></Card>}
      {tab === "add_contact" && (
        <Card>
          <form className="grid max-w-xl gap-3" onSubmit={async (e) => {
            e.preventDefault();
            const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v));
            await post("/outreach/contacts", body).catch((err) => alert(err.message));
            setForm({ email: "", name: "", organization: "", role: "", source_url: "", notes: "" });
            setTab("contacts");
            reload();
          }}>
            {(["email", "name", "organization", "role", "source_url"] as const).map((k) => (
              <input key={k} required={k === "email"} placeholder={k.replace("_", " ")} value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700" />
            ))}
            <textarea placeholder="Why this person is relevant (public information only)" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })}
              className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 text-sm dark:border-zinc-700" />
            <div><Button type="submit" variant="primary">Add and draft</Button></div>
          </form>
        </Card>
      )}
    </>
  );
}
