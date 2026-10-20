import { useEffect, useRef, useState } from "react";

import { api } from "./api";
import type { ModelMeta } from "./types";

// Model metadata is translated server-side, so the cache is cleared when the
// interface language changes (see clearMetaCache in App).
const cache = new Map<string, ModelMeta>();

export function clearMetaCache() {
  cache.clear();
}

export function useModelMeta(app?: string, model?: string) {
  const key = `${app}.${model}`;
  const [meta, setMeta] = useState<ModelMeta | null>(cache.get(key) ?? null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    if (!app || !model) return;
    const cached = cache.get(key);
    setMeta(cached ?? null);
    setError("");
    api<ModelMeta>(`/meta/models/${app}/${model}`)
      .then((m) => {
        cache.set(key, m);
        setMeta(m);
      })
      .catch((e: Error) => setError(e.message));
  }, [app, model, key]);

  return { meta, error };
}

/** Keyboard shortcuts: keys like "mod+s" (Ctrl or ⌘), "/", "?", "escape". A plain
 * key never fires while the user types in a field; a "mod+" one always does. */
export function useShortcuts(map: Record<string, (e: KeyboardEvent) => void>, enabled = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      const key = (mod ? "mod+" : "") + (e.shiftKey && e.key.length > 1 ? "shift+" : "") + e.key.toLowerCase();
      const fn = ref.current[key];
      if (!fn) return;
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
      if (typing && !mod) return;
      e.preventDefault();
      fn(e);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [enabled]);
}
