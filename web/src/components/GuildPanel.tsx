// Settings, General, "Guild & bot": the guild logo (used in the dashboard),
// and the bot's own name, avatar and server nickname in Discord
// (webapi/branding_api.py). The guild name is a normal setting below it.
import { useEffect, useRef, useState } from "react";
import { ImageUp, Trash2 } from "lucide-react";
import { ApiError, api, type Branding } from "../lib/api";
import { useLive } from "../lib/live";
import { useToast } from "./Toast";
import { Button } from "./ui";

interface Profile {
  username: string | null;
  avatar_url: string | null;
  nickname: string | null;
  can_change_nickname: boolean;
  can_edit: boolean;
}

const LOGO_MAX = 1024 * 1024;
const AVATAR_MAX = 5 * 1024 * 1024;

function readImage(file: File, max: number): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return reject(new Error("Use a PNG, JPEG, GIF or WebP image."));
    if (file.size > max) return reject(new Error(`The image is too big (at most ${max / 1024 / 1024} MB).`));
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("Couldn't read that file."));
    reader.readAsDataURL(file);
  });
}

function ImagePick({ onPick, label, busy, disabled }: { onPick: (f: File) => void; label: string; busy?: boolean; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) onPick(file);
        }}
      />
      <Button size="sm" icon={<ImageUp className="h-3.5 w-3.5" />} loading={busy} disabled={disabled} onClick={() => input.current?.click()}>
        {label}
      </Button>
    </>
  );
}

export function GuildPanel() {
  const toast = useToast();
  const { status, refreshStatus } = useLive();
  const branding = status?.branding;
  const [busy, setBusy] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [username, setUsername] = useState("");
  const [nickname, setNickname] = useState("");

  const loadProfile = () =>
    api.get<Profile>("/api/bot/profile").then((p) => {
      setProfile(p);
      setUsername(p.username ?? "");
      setNickname(p.nickname ?? "");
    });
  useEffect(() => {
    loadProfile().catch(() => undefined);
  }, []);

  const run = async (key: string, action: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await action();
      toast("ok", done);
      refreshStatus();
    } catch (e) {
      toast("error", e instanceof ApiError || e instanceof Error ? e.message : "That didn't work");
    } finally {
      setBusy(null);
    }
  };

  const uploadLogo = (file: File) =>
    run("logo", async () => api.put<Branding>("/api/branding/logo", { data: await readImage(file, LOGO_MAX) }), "Logo saved.");

  const saveProfile = (body: Record<string, string>, done: string) =>
    run("profile", async () => {
      const p = await api.put<Profile>("/api/bot/profile", body);
      setProfile(p);
      setUsername(p.username ?? "");
      setNickname(p.nickname ?? "");
    }, done);

  const profileDirty = profile && (username.trim() !== (profile.username ?? "") || nickname.trim() !== (profile.nickname ?? ""));

  return (
    <div className="mb-3 grid gap-3 xl:grid-cols-2">
      <section className="card p-4 md:p-5">
        <h3 className="font-semibold">Guild logo</h3>
        <p className="mt-1 text-muted">Shown in the dashboard and on the login page. Square images work best. Without one, the Discord server's icon is used.</p>
        <div className="mt-4 flex items-center gap-4">
          {branding?.logo_url ? (
            <img src={branding.logo_url} alt="" className="h-16 w-16 shrink-0 rounded-full object-cover ring-1 ring-line" />
          ) : (
            <div className="h-16 w-16 shrink-0 rounded-full bg-raised ring-1 ring-line" />
          )}
          <div className="flex flex-wrap gap-2">
            <ImagePick label={branding?.custom_logo ? "Replace" : "Upload"} onPick={uploadLogo} busy={busy === "logo"} />
            {branding?.custom_logo && (
              <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} disabled={busy !== null}
                onClick={() => run("logo", () => api.del("/api/branding/logo"), "Logo removed.")}>
                Remove
              </Button>
            )}
          </div>
        </div>
        <p className="mt-3 text-2xs text-faint">PNG, JPEG, GIF or WebP, up to 1 MB.</p>
      </section>

      <section className="card p-4 md:p-5">
        <h3 className="font-semibold">The bot in Discord</h3>
        <p className="mt-1 text-muted">
          The bot's name and picture everywhere, and its nickname in this server.
          {profile && !profile.can_edit && " Only admins can change them."}
        </p>
        {profile ? (
          <>
            <div className="mt-4 flex items-center gap-4">
              {profile.avatar_url && <img src={profile.avatar_url} alt="" className="h-16 w-16 shrink-0 rounded-full ring-1 ring-line" />}
              <div className="flex flex-wrap gap-2">
                <ImagePick
                  label="New picture"
                  disabled={!profile.can_edit || busy !== null}
                  busy={busy === "profile"}
                  onPick={async (file) => {
                    try {
                      saveProfile({ avatar: await readImage(file, AVATAR_MAX) }, "The bot's picture was changed.");
                    } catch (e) {
                      toast("error", (e as Error).message);
                    }
                  }}
                />
                {branding?.custom_logo && (
                  <Button size="sm" variant="ghost" disabled={!profile.can_edit || busy !== null}
                    onClick={() => saveProfile({ avatar: "guild_logo" }, "The bot now uses the guild logo.")}>
                    Use the guild logo
                  </Button>
                )}
              </div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="label mb-1 block">Name</span>
                <input className="input" value={username} maxLength={32} disabled={!profile.can_edit} onChange={(e) => setUsername(e.target.value)} />
              </label>
              <label className="block">
                <span className="label mb-1 block">Nickname in this server</span>
                <input
                  className="input"
                  value={nickname}
                  maxLength={32}
                  placeholder="Same as the name"
                  disabled={!profile.can_edit || !profile.can_change_nickname}
                  onChange={(e) => setNickname(e.target.value)}
                />
              </label>
            </div>
            {!profile.can_change_nickname && <p className="mt-2 text-xs text-warn">Give the bot the Change Nickname permission to set a nickname.</p>}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="mr-auto text-2xs text-faint">Discord allows changing the name twice an hour.</span>
              {profileDirty && (
                <Button size="sm" variant="primary" loading={busy === "profile"}
                  onClick={() => saveProfile({ username: username.trim(), nickname: nickname.trim() }, "Saved.")}>
                  Save
                </Button>
              )}
            </div>
          </>
        ) : (
          <div className="mt-4 h-16 animate-pulse rounded-lg bg-raised" />
        )}
      </section>
    </div>
  );
}
