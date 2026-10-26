"use client";
import { useState } from "react";
import { Badge, Button, Card, Drawer, Empty, ErrorNote, Field, Modal, PageHeader, Skeleton } from "@/components/ui";
import { ago, del, fmtTime, patch, post, useApi } from "@/lib/api";

type Tool = { label: string; args: string; needs?: string };
type Meta = { tools: Record<string, Tool>; templates: Record<string, any> };

function ToolPicker({ tools, value, onChange }: { tools: Record<string, Tool>; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="grid gap-1 sm:grid-cols-2">
      {Object.entries(tools).map(([k, t]) => (
        <label key={k} className="flex items-start gap-2 rounded-md p-1 text-sm hover:bg-zinc-50 dark:hover:bg-zinc-800">
          <input type="checkbox" className="mt-1" checked={value.includes(k)}
            onChange={(e) => onChange(e.target.checked ? [...value, k] : value.filter((x) => x !== k))} />
          <span>{t.label}{t.needs && <span className="ml-1 text-xs text-zinc-400">(needs {t.needs})</span>}</span>
        </label>
      ))}
    </div>
  );
}

const SCHEDULE_HINT = "Leave empty to act only when you message it. Or: every 6h · at 08:30 · at 19:00 on mon,thu (shortest: every 15m)";

function NewAgent({ meta, onClose, onDone }: { meta: Meta; onClose: () => void; onDone: () => void }) {
  const [step, setStep] = useState(1);
  const [f, setF] = useState<any>({ template: null, name: "", emoji: "🤖", token: "", task: "", tools: [], schedule: "", model_tier: "standard" });
  const [err, setErr] = useState<string | null>(null);
  const pick = (key: string | null) => {
    const t = key ? meta.templates[key] : null;
    setF({ ...f, template: key, name: t?.name ?? "", emoji: t?.emoji ?? "🤖", task: t?.task ?? "", tools: t?.tools ?? [],
           schedule: t?.schedule ?? "", model_tier: t?.model_tier ?? "standard" });
    setStep(2);
  };
  return (
    <Modal open onClose={onClose} wide title={["", "Pick a starting point", "Create its Telegram bot", "Describe its job"][step]}>
      {step === 1 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {Object.entries(meta.templates).map(([k, t]) => (
            <button key={k} onClick={() => pick(k)} className="rounded-lg border border-zinc-200 p-3 text-left hover:border-zinc-400 dark:border-zinc-700">
              <div className="font-medium">{t.emoji} {t.name}</div>
              <div className="mt-1 line-clamp-3 text-xs text-zinc-500">{t.task}</div>
            </button>
          ))}
          <button onClick={() => pick(null)} className="rounded-lg border border-dashed border-zinc-300 p-3 text-left hover:border-zinc-400 dark:border-zinc-700">
            <div className="font-medium">＋ Blank agent</div><div className="mt-1 text-xs text-zinc-500">Write its job from scratch.</div>
          </button>
        </div>
      )}
      {step === 2 && (
        <div className="space-y-3 text-sm">
          <ol className="list-decimal space-y-1 pl-5 text-zinc-600 dark:text-zinc-400">
            <li>Open <a className="text-sky-600 hover:underline" href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> in Telegram and send <code>/newbot</code>.</li>
            <li>Give it a name (e.g. “{f.name || "My Scout"}”) and a username ending in <code>bot</code>.</li>
            <li>Copy the token it sends (it looks like <code>123456789:AA…</code>) and paste it below.</li>
            <li>Open your new bot and press Start. It only ever answers you.</li>
          </ol>
          <Field label="Bot token" hint="Checked with Telegram, then stored encrypted. It is never shown again, here or anywhere.">
            <input className="field font-mono" autoComplete="off" spellCheck={false} value={f.token} onChange={(e) => setF({ ...f, token: e.target.value.trim() })} placeholder="123456789:AA…" />
          </Field>
          <div className="flex justify-between"><Button onClick={() => setStep(1)}>Back</Button>
            <Button variant="primary" disabled={!/^\d{5,15}:[A-Za-z0-9_-]{30,64}$/.test(f.token)} onClick={() => setStep(3)}>Next</Button></div>
        </div>
      )}
      {step === 3 && (
        <form className="space-y-3" onSubmit={async (e) => {
          e.preventDefault(); setErr(null);
          try { await post("/agents", { ...f, schedule: f.schedule || null }); onDone(); } catch (x: any) { setErr(x.message); }
        }}>
          <div className="grid grid-cols-[5rem_1fr] gap-2">
            <Field label="Emoji"><input className="field text-center" value={f.emoji} onChange={(e) => setF({ ...f, emoji: e.target.value })} /></Field>
            <Field label="Name"><input className="field" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          </div>
          <Field label="Its job, in your words" hint="This is its standing instruction. It can't change it; you can, any time.">
            <textarea className="field h-32" value={f.task} onChange={(e) => setF({ ...f, task: e.target.value })} />
          </Field>
          <Field label="What it may do"><ToolPicker tools={meta.tools} value={f.tools} onChange={(tools) => setF({ ...f, tools })} /></Field>
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Schedule" hint={SCHEDULE_HINT}><input className="field" value={f.schedule} onChange={(e) => setF({ ...f, schedule: e.target.value })} placeholder="at 08:30" /></Field>
            <Field label="Model" hint="Standard writes better; cheap saves your quota.">
              <select className="field" value={f.model_tier} onChange={(e) => setF({ ...f, model_tier: e.target.value })}>
                <option value="standard">Standard</option><option value="cheap">Cheap</option></select>
            </Field>
          </div>
          {err && <p className="text-sm text-rose-600">{err}</p>}
          <div className="flex justify-between"><Button onClick={() => setStep(2)}>Back</Button>
            <Button type="submit" variant="primary" disabled={f.name.length < 2 || f.task.length < 10}>Create agent</Button></div>
        </form>
      )}
    </Modal>
  );
}

function EditAgent({ agent, meta, onClose, onDone }: { agent: any; meta: Meta; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<any>({ name: agent.name, emoji: agent.emoji, task: agent.task, tools: agent.tools, schedule: agent.schedule ?? "", model_tier: agent.model_tier, token: "" });
  const [err, setErr] = useState<string | null>(null);
  const msgs = useApi<any[]>(`/agents/${agent.id}/messages`);
  return (
    <Drawer open onClose={onClose} title={`${agent.emoji} ${agent.name}`}>
      <form className="space-y-3" onSubmit={async (e) => {
        e.preventDefault(); setErr(null);
        const body: any = { ...f };
        if (!body.token) delete body.token;
        if (agent.builtin) { delete body.name; delete body.token; }
        try { await patch(`/agents/${agent.id}`, body); onDone(); } catch (x: any) { setErr(x.message); }
      }}>
        {!agent.builtin && <div className="grid grid-cols-[5rem_1fr] gap-2">
          <Field label="Emoji"><input className="field text-center" value={f.emoji} onChange={(e) => setF({ ...f, emoji: e.target.value })} /></Field>
          <Field label="Name"><input className="field" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        </div>}
        <Field label="Its job"><textarea className="field h-32" value={f.task} onChange={(e) => setF({ ...f, task: e.target.value })} /></Field>
        <Field label="What it may do"><ToolPicker tools={meta.tools} value={f.tools} onChange={(tools) => setF({ ...f, tools })} /></Field>
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Schedule" hint={SCHEDULE_HINT}><input className="field" value={f.schedule} onChange={(e) => setF({ ...f, schedule: e.target.value })} /></Field>
          <Field label="Model"><select className="field" value={f.model_tier} onChange={(e) => setF({ ...f, model_tier: e.target.value })}>
            <option value="standard">Standard</option><option value="cheap">Cheap</option></select></Field>
        </div>
        {!agent.builtin && <Field label="Replace bot token" hint={`Current: ${agent.token_hint ?? "none"}. Leave empty to keep it.`}>
          <input className="field font-mono" autoComplete="off" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value.trim() })} /></Field>}
        {err && <p className="text-sm text-rose-600">{err}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary">Save</Button>
          <Button onClick={() => post(`/agents/${agent.id}/forget`).then(() => msgs.reload())}>Clear its memory</Button>
          {!agent.builtin && <Button variant="danger" onClick={async () => { if (confirm(`Delete ${agent.name}? Its bot stops answering.`)) { await del(`/agents/${agent.id}`); onDone(); } }}>Delete</Button>}
        </div>
      </form>
      <h3 className="mb-2 mt-6 text-sm font-semibold">Recent conversation</h3>
      {!msgs.data ? <Skeleton /> : !msgs.data.length ? <Empty>No messages yet.</Empty> : (
        <ul className="space-y-2 text-sm">
          {msgs.data.map((m, i) => (
            <li key={i} className={`max-w-[85%] whitespace-pre-line rounded-lg px-3 py-2 ${m.role === "owner" ? "ml-auto bg-sky-50 dark:bg-sky-950" : "bg-zinc-100 dark:bg-zinc-800"}`}>
              {m.text}<div className="mt-1 text-[10px] text-zinc-400">{fmtTime(m.ts)}</div></li>
          ))}
        </ul>
      )}
    </Drawer>
  );
}

export default function Agents() {
  const { data, error, reload } = useApi<any>("/agents");
  const [creating, setCreating] = useState(false);
  const [edit, setEdit] = useState<any>(null);
  return (
    <>
      <PageHeader title="Agents" subtitle="Each agent is its own Telegram bot with one job. They only talk to you, only use the tools you tick, and drafts are never sent."
        actions={<Button variant="primary" onClick={() => setCreating(true)} disabled={!data}>＋ New agent</Button>} />
      <ErrorNote error={error} />
      {!data ? <Skeleton /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.agents.map((a: any) => (
            <Card key={a.id}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="text-base font-semibold">{a.emoji} {a.name}</div>
                  <div className="text-xs text-zinc-500">{a.builtin ? "on your main NOESIS bot" : a.telegram_username ? <a className="hover:underline" href={`https://t.me/${a.telegram_username}`} target="_blank" rel="noreferrer">@{a.telegram_username}</a> : "no bot"}</div>
                </div>
                <Badge tone={a.enabled ? "green" : "gray"}>{a.enabled ? "on" : "off"}</Badge>
              </div>
              <p className="mt-2 line-clamp-3 text-sm text-zinc-600 dark:text-zinc-400">{a.task}</p>
              <div className="mt-2 flex flex-wrap gap-1">{a.tools.map((t: string) => <Badge key={t} tone="blue">{data.tools[t]?.label ?? t}</Badge>)}</div>
              <div className="mt-2 text-xs text-zinc-500">
                {a.schedule ? `${a.schedule} · next ${fmtTime(a.next_run)}` : "acts when you message it"} · active {ago(a.last_active)} · {a.messages_24h} msgs today
              </div>
              {a.last_error && <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-300">{a.last_error}</p>}
              <div className="mt-3 flex flex-wrap gap-2">
                <Button onClick={() => setEdit(a)}>Edit</Button>
                <Button onClick={() => patch(`/agents/${a.id}`, { enabled: !a.enabled }).then(reload)}>{a.enabled ? "Turn off" : "Turn on"}</Button>
                {a.enabled && <Button onClick={() => post(`/agents/${a.id}/run`).then(() => alert("Running now; it replies in Telegram."))}>Run now</Button>}
              </div>
            </Card>
          ))}
        </div>
      )}
      <p className="mt-6 text-xs text-zinc-500">Tokens are encrypted with NOESIS_SECRET_KEY on the server. If agents show “can't be decrypted”, the key changed: paste their tokens again.</p>
      {creating && data && <NewAgent meta={data} onClose={() => setCreating(false)} onDone={() => { setCreating(false); reload(); }} />}
      {edit && data && <EditAgent agent={edit} meta={data} onClose={() => setEdit(null)} onDone={() => { setEdit(null); reload(); }} />}
    </>
  );
}

