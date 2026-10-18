import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import { useI18n } from "../i18n";
import type { Choice, Scalar } from "./model";

type Props = {
  model: string; // semantic model name
  dimension: string;
  multiple: boolean;
  value: Scalar | Scalar[] | null | undefined;
  onChange: (value: Scalar | Scalar[]) => void;
};

/** Search-as-you-type over a dimension's values (GET /reports/values). */
export default function ValuePicker({ model, dimension, multiple, value, onChange }: Props) {
  const { t, fmt } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Choice[]>([]);
  const [more, setMore] = useState(false);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const box = useRef<HTMLDivElement>(null);
  const selected: Scalar[] = Array.isArray(value) ? value : value === "" || value == null ? [] : [value];

  useEffect(() => {
    if (!open && selected.length === 0) return;
    const timer = setTimeout(() => {
      api<{ values: Choice[]; more: boolean }>("/reports/values", {
        params: { semantic_model: model, dimension, q: open ? query : "" },
      })
        .then((r) => {
          setResults(r.values);
          setMore(r.more);
          setLabels((old) => ({ ...old, ...Object.fromEntries(r.values.map((v) => [String(v.value), v.label])) }));
        })
        .catch(() => setResults([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [open, query, model, dimension]);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const text = (v: Scalar) => labels[String(v)] ?? fmt.digits(String(v));
  const choose = (v: Scalar) => {
    if (multiple) {
      if (!selected.some((s) => s === v)) onChange([...selected, v]);
    } else {
      onChange(v);
      setOpen(false);
    }
  };

  return (
    <div className="relation-select value-picker" ref={box}>
      {multiple && selected.length > 0 && (
        <ul className="chips">
          {selected.map((v) => (
            <li key={String(v)}>
              <bdi>{text(v)}</bdi>
              <button type="button" aria-label={t("common.remove")} onClick={() => onChange(selected.filter((s) => s !== v))}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <button type="button" className="relation-value" onClick={() => setOpen((o) => !o)}>
        <bdi className={!multiple && selected.length ? "" : "muted"}>
          {!multiple && selected.length ? text(selected[0]) : t("relation.choose")}
        </bdi>
        <span aria-hidden>▾</span>
      </button>
      {open && (
        <div className="relation-popup">
          <input
            dir="auto"
            autoFocus
            placeholder={t("common.searchPlaceholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <ul>
            {results.map((r) => (
              <li key={String(r.value)}>
                <button type="button" onClick={() => choose(r.value)}>
                  <bdi>{r.label === r.value ? fmt.digits(String(r.label)) : r.label}</bdi>
                </button>
              </li>
            ))}
            {!results.length && <li className="muted">{t("relation.noResults")}</li>}
            {more && <li className="muted">{t("relation.more")}</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
