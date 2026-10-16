"use client";
import { Badge, Card, ErrorNote, PageHeader, Table } from "@/components/ui";
import { ago, useApi } from "@/lib/api";

const HOW: Record<string, string> = {
  telegram: "Set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID (your numeric user id).",
  gmail: "Run scripts/google_oauth.py once, then set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN.",
  calendar: "Uses the same Google OAuth credentials as Gmail.",
  linkedin: "Set LINKEDIN_ACCESS_TOKEN and LINKEDIN_AUTHOR_URN from a LinkedIn developer app you own.",
  instagram: "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID (Business or Creator account via Meta Graph API).",
  obsidian: "Set NOESIS_VAULT_PATH to the server-side working copy of your vault (synced with Syncthing).",
};

export default function Integrations() {
  const { data, error } = useApi<any[]>("/integrations");
  return (
    <>
      <PageHeader title="Integrations" subtitle="Status reflects real checks. “not configured” means credentials are missing; nothing is ever shown as connected unless it is." />
      <ErrorNote error={error} />
      <Card>
        <Table rows={data} rowKey="name" cols={[
          { key: "name", label: "Integration", render: (i) => <span className="font-medium capitalize">{i.name}</span> },
          { key: "status", label: "Status", render: (i) => <Badge>{i.status}</Badge> },
          { key: "detail", label: "Detail", render: (i) => <span className="text-xs text-zinc-500">{i.detail}</span> },
          { key: "last_ok", label: "Last OK", render: (i) => ago(i.last_ok) },
          { key: "how", label: "How to connect", render: (i) => <span className="text-xs">{HOW[i.name] ?? ""}</span> },
        ]} />
      </Card>
    </>
  );
}
