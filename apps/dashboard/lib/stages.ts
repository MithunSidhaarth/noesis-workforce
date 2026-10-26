export const STAGE_LABEL: Record<string, string> = {
  found: "Found", shortlisted: "Shortlisted", preparing: "Preparing", applied: "Applied",
  online_assessment: "Online assessment", interview_1: "Interview 1", interview_2: "Interview 2", hr: "HR round",
  offer: "Offer 🎉", rejected: "Rejected", ghosted: "No reply", withdrawn: "Withdrawn", skipped: "Skipped",
};
export const EVENT_STAGES = ["online_assessment", "interview_1", "interview_2", "hr"];
export const STAGE_TONE: Record<string, "green" | "blue" | "amber" | "red" | "violet" | "gray"> = {
  shortlisted: "blue", preparing: "blue", applied: "violet", online_assessment: "amber", interview_1: "amber",
  interview_2: "amber", hr: "amber", offer: "green", rejected: "red", ghosted: "gray", withdrawn: "gray", skipped: "gray", found: "gray",
};

/** "2026-11-04T15:00" (local input) -> unix seconds */
export const toTs = (v: string) => (v ? Math.round(new Date(v).getTime() / 1000) : undefined);
export const inr = (n?: number | null) => (n ? `₹${n.toLocaleString("en-IN")}` : "—");
