"use client";
import { Card, ErrorNote, PageHeader, Stat, Table } from "@/components/ui";
import { useApi } from "@/lib/api";

export default function Usage() {
  const { data, error } = useApi<any>("/usage?days=30");
  const max = Math.max(0.0001, ...(data?.by_day ?? []).map((d: any) => d.cost_usd ?? 0));
  return (
    <>
      <PageHeader title="Usage & cost" subtitle="Model calls and spend recorded per bot. Claude Pro does not cover these API calls; they bill to whichever provider key you configure." />
      <ErrorNote error={error} />
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Spend (30d)" value={`$${(data?.total_cost_usd ?? 0).toFixed(4)}`} />
        <Stat label="Model calls" value={(data?.by_bot ?? []).reduce((a: number, b: any) => a + b.calls, 0)} />
        <Stat label="Cached" value={(data?.by_bot ?? []).reduce((a: number, b: any) => a + (b.cached ?? 0), 0)} />
        <Stat label="Errors" value={(data?.by_bot ?? []).reduce((a: number, b: any) => a + (b.errors ?? 0), 0)} />
      </div>
      <Card title="Daily spend" className="mb-6">
        {data?.by_day?.length ? (
          <div className="flex h-40 items-end gap-1">
            {data.by_day.map((d: any) => (
              <div key={d.day} className="flex flex-1 flex-col items-center gap-1" title={`${d.day}: $${d.cost_usd} · ${d.calls} calls`}>
                <div className="w-full rounded-t bg-zinc-800 dark:bg-zinc-200" style={{ height: `${Math.max(2, (d.cost_usd / max) * 140)}px` }} />
                <span className="text-[10px] text-zinc-500">{d.day.slice(5)}</span>
              </div>
            ))}
          </div>
        ) : <p className="text-sm text-zinc-500">No model calls yet.</p>}
      </Card>
      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="By bot"><Table rows={data?.by_bot} rowKey="bot_id" cols={[
          { key: "bot_id", label: "Bot" }, { key: "calls", label: "Calls" },
          { key: "tokens", label: "Tokens in/out", render: (r) => `${r.input_tokens ?? 0} / ${r.output_tokens ?? 0}` },
          { key: "cost_usd", label: "Cost", render: (r) => `$${(r.cost_usd ?? 0).toFixed(4)}` },
        ]} /></Card>
        <Card title="By model"><Table rows={data?.by_model} rowKey="model" cols={[
          { key: "provider", label: "Provider" }, { key: "model", label: "Model" }, { key: "calls", label: "Calls" },
          { key: "cost_usd", label: "Cost", render: (r) => `$${(r.cost_usd ?? 0).toFixed(4)}` },
        ]} /></Card>
        <Card title="External actions" className="xl:col-span-2"><Table rows={data?.actions} rowKey="action" cols={[
          { key: "action", label: "Action" }, { key: "status", label: "Result" }, { key: "n", label: "Count" },
        ]} /></Card>
      </div>
    </>
  );
}
