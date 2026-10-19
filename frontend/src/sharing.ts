/** Saved reports, dashboards and their sharing (bi_admin.sharing). */
import { api } from "./api";
import type { Spec } from "./reports/model";

export type Visibility = "private" | "users" | "everyone";
export type Person = { id: number; username: string; full_name: string };
export type GroupRef = { id: number; name: string };
export type Sharing = { visibility: Visibility; users: Person[]; groups: GroupRef[] };

/** What the API accepts: ids instead of objects. */
export type SharingIn = { visibility: Visibility; users: number[]; groups: number[] };
export const sharingIn = (s: Sharing): SharingIn => ({
  visibility: s.visibility,
  users: s.users.map((u) => u.id),
  groups: s.groups.map((g) => g.id),
});

export type Shared = {
  id: number;
  name: string;
  description: string;
  owner: Person;
  is_owner: boolean;
  can_edit: boolean;
  sharing: Sharing;
  created_at: string;
  updated_at: string;
};

export type SavedReport = Shared & {
  semantic_model: string;
  spec: Spec;
  available?: boolean;
  problem?: string;
};

export const personName = (p: Person | null | undefined) => (p ? p.full_name || p.username : "");

export const listSaved = (params?: Record<string, string>) =>
  api<{ saved_reports: SavedReport[] }>("/saved-reports", { params }).then((r) => r.saved_reports);

export const getSaved = (id: number | string) => api<SavedReport>(`/saved-reports/${id}`);

export const createSaved = (body: { name: string; description: string; spec: Spec; sharing: SharingIn }) =>
  api<SavedReport>("/saved-reports", { method: "POST", body });

export const updateSaved = (id: number, body: Partial<{ name: string; description: string; spec: Spec; sharing: SharingIn }>) =>
  api<SavedReport>(`/saved-reports/${id}`, { method: "PATCH", body });

export const deleteSaved = (id: number) => api<{ deleted: boolean }>(`/saved-reports/${id}`, { method: "DELETE" });

export const duplicateSaved = (id: number) => api<SavedReport>(`/saved-reports/${id}/duplicate`, { method: "POST" });

export const shareTargets = (q: string) =>
  api<{ users: Person[]; groups: GroupRef[] }>("/sharing/targets", { params: { q } });

/** First error message of a 400 with per-field errors, else the detail. */
export function errorText(err: unknown): string {
  const data = (err as { data?: { errors?: unknown; detail?: string } })?.data;
  if (data?.errors && typeof data.errors === "object" && !Array.isArray(data.errors)) {
    const all = Object.values(data.errors as Record<string, string[]>).flat();
    if (all.length) return all.join(" ");
  }
  if (Array.isArray(data?.errors)) {
    return (data.errors as { message?: string }[]).map((e) => e.message).join(" ");
  }
  return err instanceof Error ? err.message : String(err);
}
