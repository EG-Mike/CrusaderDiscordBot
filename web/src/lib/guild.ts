// The server's roles and channels, for pickers and mention previews.
import { useEffect, useState } from "react";
import { api, type Channel, type Role } from "./api";

export function useGuildLookups() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  useEffect(() => {
    api.get<Role[]>("/api/guild/roles").then(setRoles).catch(() => undefined);
    api.get<Channel[]>("/api/guild/channels").then(setChannels).catch(() => undefined);
  }, []);
  return { roles, channels };
}
