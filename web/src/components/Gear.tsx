// A character's gear, talents, statistics and professions from Blizzard
// (gear.py), shared by the Character lookup and Applications pages.
import { Gem, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import type { CardStatistics, CharacterSummary, GearItem, StatGroup } from "../lib/api";
import { QUALITY_COLORS } from "../lib/wow";

export const COSMETIC_SLOTS = new Set(["SHIRT", "TABARD"]);

export function ItemRow({ item }: { item: GearItem }) {
  const extras = [...item.enchants, ...item.sockets.map((s) => s.gem ?? `Empty ${s.type ?? "socket"}`)];
  return (
    <li className="flex items-start gap-3 px-4 py-2 md:px-5">
      {item.icon_url ? (
        <img src={item.icon_url} alt="" className="mt-0.5 h-8 w-8 shrink-0 rounded ring-1 ring-line" loading="lazy" />
      ) : (
        <div className="mt-0.5 h-8 w-8 shrink-0 rounded bg-raised ring-1 ring-line" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {item.url ? (
            <a href={item.url} target="_blank" rel="noreferrer" className="font-medium hover:underline" style={{ color: QUALITY_COLORS[item.quality ?? ""] }}>
              {item.name}
            </a>
          ) : (
            <span className="font-medium" style={{ color: QUALITY_COLORS[item.quality ?? ""] }}>
              {item.name}
            </span>
          )}
          <span className="text-2xs text-faint">{item.slot_name}</span>
          {item.set && <span className="text-2xs text-gold">{item.set}</span>}
        </div>
        {extras.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-2xs">
            {item.enchants.map((e) => (
              <span key={e} className="inline-flex items-center gap-1 text-ok">
                <Sparkles className="h-3 w-3" /> {e}
              </span>
            ))}
            {item.sockets.map((s, i) => (
              <span key={i} className={s.gem ? "inline-flex items-center gap-1 text-info" : "inline-flex items-center gap-1 text-warn"}>
                <Gem className="h-3 w-3" /> {s.gem ?? `Empty ${s.type ?? "socket"}`}
              </span>
            ))}
          </div>
        )}
        {item.stats.length > 0 && <div className="mt-0.5 truncate text-2xs text-faint">{item.stats.join(" · ")}</div>}
      </div>
      <span className="shrink-0 font-semibold tabular-nums">{item.item_level ?? "?"}</span>
    </li>
  );
}

export function GearCard({ items }: { items: GearItem[] }) {
  const worn = items.filter((i) => !COSMETIC_SLOTS.has(i.slot));
  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Gear</div>
      </header>
      <ul className="divide-y divide-line">
        {worn.map((item) => (
          <ItemRow key={item.slot} item={item} />
        ))}
        {worn.length === 0 && <li className="px-4 py-3 text-faint md:px-5">No gear equipped.</li>}
      </ul>
    </section>
  );
}

export function TalentsCard({ talents }: { talents: CharacterSummary["talents"] }) {
  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Talents</div>
      </header>
      <ul className="divide-y divide-line">
        {talents.map((t) => (
          <li key={t.spec} className="flex items-center justify-between px-4 py-2 md:px-5">
            {t.spec}
            <span className="font-semibold tabular-nums">{t.points}</span>
          </li>
        ))}
        {talents.length === 0 && <li className="px-4 py-3 text-faint md:px-5">No talents spent.</li>}
      </ul>
    </section>
  );
}

export function ProfessionsCard({ professions }: { professions: CharacterSummary["professions"] }) {
  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Professions</div>
      </header>
      <ul className="divide-y divide-line">
        {professions.map((p) => (
          <li key={p.name} className="flex items-center justify-between px-4 py-2 md:px-5">
            <span>
              {p.name} {!p.primary && <span className="text-2xs text-faint">secondary</span>}
            </span>
            <span className="tabular-nums text-muted">
              {p.skill ?? "?"} / {p.max ?? "?"}
            </span>
          </li>
        ))}
        {professions.length === 0 && <li className="px-4 py-3 text-faint md:px-5">None.</li>}
      </ul>
    </section>
  );
}

/** The character sheet, grouped (General, Melee, Spell, Defense, ...). */
export function StatsCard({ groups }: { groups: StatGroup[] }) {
  return (
    <section className="card">
      <header className="border-b border-line px-4 py-3 md:px-5">
        <div className="label">Character stats</div>
      </header>
      {groups.length === 0 ? (
        <p className="px-4 py-3 text-faint md:px-5">Blizzard didn't send statistics for this character.</p>
      ) : (
        <div className="grid gap-x-6 gap-y-3 px-4 py-3 sm:grid-cols-2 md:px-5">
          {groups.map((g) => (
            <div key={g.title}>
              <div className="mb-1 text-2xs font-semibold uppercase tracking-wide text-faint">{g.title}</div>
              <dl className="space-y-0.5">
                {g.stats.map((s) => (
                  <div key={s.key} className="flex items-baseline justify-between gap-3">
                    <dt className="text-muted">{s.label}</dt>
                    <dd className="font-medium tabular-nums">{s.text}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** The achievement statistics moderators chose for the card (e.g. raid boss kills). */
export function CardStatisticsCard({ statistics }: { statistics: CardStatistics }) {
  return (
    <section className="card">
      <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3 md:px-5">
        <div className="label">{statistics.title || "Statistics"}</div>
        <Link to="/applications/statistics" className="text-2xs text-gold hover:underline">
          Choose
        </Link>
      </header>
      {statistics.groups.length === 0 ? (
        <p className="px-4 py-3 text-faint md:px-5">No statistics chosen for the card yet.</p>
      ) : (
        <div className="space-y-2 px-4 py-3 md:px-5">
          {statistics.groups.map((g, i) => (
            <div key={`${g.group}-${i}`}>
              {g.group && <div className="mb-0.5 text-2xs font-semibold uppercase tracking-wide text-faint">{g.group}</div>}
              <div className="flex flex-wrap gap-1.5">
                {g.items.map((item) => (
                  <span key={item.label} className={`inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-0.5 text-xs ${item.value ? "bg-raised/40" : "opacity-50"}`}>
                    {item.label}
                    <span className="font-semibold tabular-nums">{item.value}</span>
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
