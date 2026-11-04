// Small shared UI building blocks.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Loader2 } from "lucide-react";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

type Tone = "neutral" | "gold" | "crimson" | "ok" | "warn" | "info";

const badgeTones: Record<Tone, string> = {
  neutral: "border-line bg-raised text-muted",
  gold: "border-gold/30 bg-gold/10 text-gold",
  crimson: "border-crimson/30 bg-crimson/10 text-crimson",
  ok: "border-ok/30 bg-ok/10 text-ok",
  warn: "border-warn/30 bg-warn/10 text-warn",
  info: "border-info/30 bg-info/10 text-info",
};

export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-px text-2xs font-semibold uppercase tracking-wider",
        badgeTones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

type Variant = "primary" | "outline" | "ghost" | "danger";

const buttonVariants: Record<Variant, string> = {
  primary: "border-gold/50 bg-gold/15 text-gold hover:bg-gold/25 hover:border-gold/70",
  outline: "border-line bg-raised/60 text-fg hover:border-faint hover:bg-raised",
  ghost: "border-transparent text-muted hover:text-fg hover:bg-raised",
  danger: "border-crimson/40 bg-crimson/10 text-crimson hover:bg-crimson/20",
};

export function Button({
  variant = "outline",
  size = "md",
  loading,
  icon,
  children,
  className,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
}) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg border font-medium transition",
        "disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/40",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-[13px]",
        buttonVariants[variant],
        className,
      )}
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/40 disabled:opacity-50",
        checked ? "border-gold/60 bg-gold/30" : "border-line bg-raised",
      )}
    >
      <span
        className={cx(
          "inline-block h-3.5 w-3.5 rounded-full transition",
          checked ? "translate-x-[18px] bg-gold" : "translate-x-[3px] bg-faint",
        )}
      />
    </button>
  );
}

export function StatTile({
  value,
  label,
  icon,
  tone,
}: {
  value: ReactNode;
  label: string;
  icon?: ReactNode;
  tone?: "ok" | "warn" | "crimson" | "gold";
}) {
  const toneText = tone ? { ok: "text-ok", warn: "text-warn", crimson: "text-crimson", gold: "text-gold" }[tone] : "text-fg";
  return (
    <div className="card flex flex-col items-center justify-center gap-1 px-4 py-4 text-center transition hover:border-faint/60">
      <div className={cx("text-2xl font-bold tabular-nums tracking-tight", toneText)}>{value}</div>
      <div className="flex items-center gap-1.5 text-2xs text-muted">
        {icon}
        {label}
      </div>
    </div>
  );
}

export function SectionHeader({ eyebrow, title, description, actions }: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        {eyebrow && <div className="label mb-1.5 text-gold/90">● {eyebrow}</div>}
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-lg bg-raised", className)} />;
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      {icon && <div className="text-faint">{icon}</div>}
      <div className="font-semibold">{title}</div>
      {children && <div className="max-w-md text-muted">{children}</div>}
    </div>
  );
}

/** Segmented tabs: one choice out of a few, e.g. a time range. */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: readonly (readonly [T, string, ...unknown[]])[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="flex shrink-0 rounded-lg border border-line bg-surface p-0.5" role="group" aria-label={label}>
      {options.map(([id, text]) => (
        <button
          key={id}
          type="button"
          aria-pressed={value === id}
          onClick={() => onChange(id)}
          className={cx(
            "rounded-md px-2.5 py-1.5 text-xs font-medium transition",
            value === id ? "bg-gold/15 text-gold" : "text-muted hover:text-fg",
          )}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
