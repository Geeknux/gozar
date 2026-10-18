import { useEffect, useState } from "react";

import { api } from "./api";
import type { ModelMeta } from "./types";

const cache = new Map<string, ModelMeta>();

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
