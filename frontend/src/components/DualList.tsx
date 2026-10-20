import { useEffect, useMemo, useState } from "react";

import { api, crudPath } from "../api";
import { useI18n } from "../i18n";
import type { Ref } from "../types";

type Props = {
  model: string;
  field: string;
  inline?: string;
  value: Ref[];
  onChange: (value: Ref[]) => void;
  vertical?: boolean;
  label: string;
};

/**
 * admin's filter_horizontal: available rows on one side, chosen ones on the other,
 * each with a filter box; move with the arrow buttons, a double click or
 * "Choose all" / "Remove all". Only rows the user may pick are offered.
 */
export default function DualList({ model, field, inline, value, onChange, vertical, label }: Props) {
  const { t, fmt } = useI18n();
  const [options, setOptions] = useState<Ref[]>([]);
  const [more, setMore] = useState(false);
  const [query, setQuery] = useState("");
  const [chosenQuery, setChosenQuery] = useState("");
  const [left, setLeft] = useState<Set<string>>(new Set());
  const [right, setRight] = useState<Set<string>>(new Set());

  useEffect(() => {
    const timer = setTimeout(() => {
      api<{ results: Ref[]; more: boolean }>(crudPath(model, "autocomplete"), {
        params: { field, q: query, limit: "200", ...(inline ? { inline } : {}) },
      })
        .then((r) => {
          setOptions(r.results);
          setMore(r.more);
        })
        .catch(() => setOptions([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [model, field, inline, query]);

  const chosenKeys = useMemo(() => new Set(value.map((v) => String(v.pk))), [value]);
  const available = options.filter((o) => !chosenKeys.has(String(o.pk)));
  const cq = chosenQuery.trim().toLocaleLowerCase();
  const chosen = cq ? value.filter((v) => v.label.toLocaleLowerCase().includes(cq)) : value;

  const add = (keys: Set<string>) => {
    onChange([...value, ...available.filter((o) => keys.has(String(o.pk)))]);
    setLeft(new Set());
  };
  const remove = (keys: Set<string>) => {
    onChange(value.filter((v) => !keys.has(String(v.pk))));
    setRight(new Set());
  };
  const toggle = (set: Set<string>, key: string, update: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    update(next);
  };

  return (
    <div className={`dual-list${vertical ? " vertical" : ""}`}>
      <div className="dual-pane">
        <div className="dual-title">{t("widget.available", { label })}</div>
        <input dir="auto" type="search" placeholder={t("widget.filter")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <ul role="listbox" aria-multiselectable="true" aria-label={t("widget.available", { label })}>
          {available.map((o) => (
            <li
              key={String(o.pk)}
              role="option"
              aria-selected={left.has(String(o.pk))}
              className={left.has(String(o.pk)) ? "selected" : ""}
              onClick={() => toggle(left, String(o.pk), setLeft)}
              onDoubleClick={() => add(new Set([String(o.pk)]))}
            >
              <bdi>{o.label}</bdi>
            </li>
          ))}
        </ul>
        {more && <small className="muted">{t("relation.more")}</small>}
        <button type="button" className="link" disabled={!available.length} onClick={() => add(new Set(available.map((o) => String(o.pk))))}>
          {t("widget.chooseAll")}
        </button>
      </div>
      <div className="dual-buttons">
        <button type="button" aria-label={t("widget.choose")} disabled={!left.size} onClick={() => add(left)}>
          <span className="dir-arrow">→</span>
        </button>
        <button type="button" aria-label={t("widget.remove")} disabled={!right.size} onClick={() => remove(right)}>
          <span className="dir-arrow">←</span>
        </button>
      </div>
      <div className="dual-pane">
        <div className="dual-title">
          {t("widget.chosen", { label })} <span className="muted">({fmt.number(value.length)})</span>
        </div>
        <input dir="auto" type="search" placeholder={t("widget.filter")} value={chosenQuery} onChange={(e) => setChosenQuery(e.target.value)} />
        <ul role="listbox" aria-multiselectable="true" aria-label={t("widget.chosen", { label })}>
          {chosen.map((o) => (
            <li
              key={String(o.pk)}
              role="option"
              aria-selected={right.has(String(o.pk))}
              className={right.has(String(o.pk)) ? "selected" : ""}
              onClick={() => toggle(right, String(o.pk), setRight)}
              onDoubleClick={() => remove(new Set([String(o.pk)]))}
            >
              <bdi>{o.label}</bdi>
            </li>
          ))}
        </ul>
        <button type="button" className="link" disabled={!value.length} onClick={() => remove(new Set(value.map((v) => String(v.pk))))}>
          {t("widget.removeAll")}
        </button>
      </div>
    </div>
  );
}
