import { Hourglass } from "lucide-react";
import { SectionHeader } from "../components/ui";

const PHASE: Record<string, string> = {
  "/applications": "Phase 3",
  "/polls": "Phase 3",
  "/raid-summaries": "Phase 4",
  "/raid-logs": "Phase 4",
  "/records": "Phase 4",
  "/attendance": "Phase 4",
  "/tier-recap": "Phase 4",
  "/maintenance": "Phase 5",
};

export default function ComingSoon({ item }: { item: { to: string; label: string } }) {
  return (
    <>
      <SectionHeader eyebrow={PHASE[item.to] ?? "Planned"} title={item.label} />
      <div className="card flex flex-col items-center gap-3 px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full border border-gold/30 bg-gold/10 text-gold">
          <Hourglass className="h-5 w-5" />
        </div>
        <div className="font-semibold">Coming in {PHASE[item.to] ?? "a later phase"}</div>
        <p className="max-w-md text-muted">
          This page isn't built yet. Until then, keep using the bot's Discord commands for {item.label.toLowerCase()}.
        </p>
      </div>
    </>
  );
}
