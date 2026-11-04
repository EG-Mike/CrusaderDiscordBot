import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle } from "lucide-react";

function DiscordIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M20.3 4.4A19.8 19.8 0 0 0 15.4 3l-.6 1.3a18.3 18.3 0 0 0-5.5 0L8.6 3a19.7 19.7 0 0 0-4.9 1.5C.6 9.1-.3 13.6.1 18.1a19.9 19.9 0 0 0 6 3l1.3-2.1a12.9 12.9 0 0 1-2-1l.5-.4a14.2 14.2 0 0 0 12.2 0l.5.4c-.6.4-1.3.7-2 1l1.3 2.1a19.8 19.8 0 0 0 6-3c.5-5.2-.8-9.7-3.6-13.7ZM8 15.4c-1.2 0-2.2-1.1-2.2-2.4S6.8 10.6 8 10.6s2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Zm8 0c-1.2 0-2.2-1.1-2.2-2.4s1-2.4 2.2-2.4 2.2 1.1 2.2 2.4-1 2.4-2.2 2.4Z" />
    </svg>
  );
}

export default function Login() {
  const [params] = useSearchParams();
  const error = params.get("error");
  // Public: the guild's own name and logo, when one was set in Settings.
  const [branding, setBranding] = useState<{ name: string | null; logo_url: string | null; custom_logo: boolean } | null>(null);
  useEffect(() => {
    fetch("/branding")
      .then((r) => (r.ok ? r.json() : null))
      .then(setBranding)
      .catch(() => undefined);
  }, []);
  return (
    <div className="app-glow flex min-h-full items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          {branding?.custom_logo && branding.logo_url ? (
            <img src={branding.logo_url} alt="" className="h-14 w-14 rounded-full object-cover ring-1 ring-line" />
          ) : (
            <img src="/crest.svg" alt="" className="h-14 w-14 drop-shadow-[0_0_24px_rgba(222,172,82,0.35)]" />
          )}
          <div>
            <div className="text-lg font-bold tracking-[0.25em]">CRUSADER</div>
            <div className="label mt-1">{branding?.name ? `${branding.name} · Bot dashboard` : "Bot dashboard"}</div>
          </div>
        </div>
        <div className="card p-6">
          <h1 className="text-base font-semibold">Sign in</h1>
          <p className="mt-1 text-muted">Use your Discord account. You need the moderator role in the server.</p>
          {error && (
            <div className="mt-4 flex gap-2 rounded-lg border border-crimson/30 bg-crimson/10 px-3 py-2.5 text-crimson">
              <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}
          <a
            href="/auth/login"
            className="mt-5 flex h-10 items-center justify-center gap-2 rounded-lg bg-[#5865F2] font-semibold text-white transition hover:bg-[#4752c4] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5865F2]/50"
          >
            <DiscordIcon className="h-5 w-5" />
            Continue with Discord
          </a>
        </div>
        <p className="mt-4 text-center text-2xs text-faint">We only read your Discord name and avatar.</p>
      </div>
    </div>
  );
}
