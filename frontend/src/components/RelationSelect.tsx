import { useEffect, useRef, useState } from "react";

import { api, crudPath } from "../api";
import type { Ref } from "../types";

type Props = {
  model: string; // label of the model that owns the field
  field: string;
  value: Ref | null;
  onChange: (value: Ref | null) => void;
  disabled?: boolean;
  placeholder?: string;
};

/** Search-as-you-type picker for FK/O2O fields (admin's autocomplete_fields). */
export default function RelationSelect({ model, field, value, onChange, disabled, placeholder }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Ref[]>([]);
  const [more, setMore] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      api<{ results: Ref[]; more: boolean }>(crudPath(model, "autocomplete"), {
        params: { field, q: query },
      })
        .then((r) => {
          setResults(r.results);
          setMore(r.more);
        })
        .catch(() => setResults([]));
    }, 200);
    return () => clearTimeout(t);
  }, [open, query, model, field]);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  return (
    <div className="relation-select" ref={box}>
      <button
        type="button"
        className="relation-value"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        <bdi>{value ? value.label : placeholder ?? "انتخاب کنید…"}</bdi>
        <span aria-hidden>▾</span>
      </button>
      {open && (
        <div className="relation-popup">
          <input
            dir="auto"
            autoFocus
            placeholder="جستجو…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ul>
            {value && (
              <li>
                <button type="button" className="muted" onClick={() => (onChange(null), setOpen(false))}>
                  — پاک کردن —
                </button>
              </li>
            )}
            {results.map((r) => (
              <li key={String(r.pk)}>
                <button type="button" onClick={() => (onChange(r), setOpen(false))}>
                  <bdi>{r.label}</bdi>
                </button>
              </li>
            ))}
            {!results.length && <li className="muted">موردی یافت نشد</li>}
            {more && <li className="muted">نتایج بیشتری هست؛ دقیق‌تر جستجو کنید.</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
