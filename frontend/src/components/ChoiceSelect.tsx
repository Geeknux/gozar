import { useEffect, useRef, useState } from "react";

import { useI18n } from "../i18n";
import type { Choice } from "../types";

type Props = {
  id?: string;
  choices: Choice[];
  value: string;
  onChange: (value: string) => void;
  allowEmpty?: boolean;
  disabled?: boolean;
};

/** A searchable select over a field's choices (the "rich_select" widget). */
export default function ChoiceSelect({ id, choices, value, onChange, allowEmpty, disabled }: Props) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const current = choices.find((c) => String(c.value) === value);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const q = query.trim().toLocaleLowerCase();
  const shown = q ? choices.filter((c) => c.label.toLocaleLowerCase().includes(q) || String(c.value).includes(q)) : choices;
  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setQuery("");
  };

  return (
    <div className="relation-select" ref={box}>
      <button id={id} type="button" className="relation-value" disabled={disabled} onClick={() => setOpen((o) => !o)}>
        <bdi>{current ? current.label : t("relation.choose")}</bdi>
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
            onKeyDown={(e) => {
              if (e.key === "Enter" && shown.length) {
                e.preventDefault();
                pick(String(shown[0].value));
              }
            }}
          />
          <ul>
            {allowEmpty && value && (
              <li>
                <button type="button" className="muted" onClick={() => pick("")}>
                  {t("relation.clear")}
                </button>
              </li>
            )}
            {shown.map((c) => (
              <li key={String(c.value)}>
                <button type="button" className={String(c.value) === value ? "active" : ""} onClick={() => pick(String(c.value))}>
                  <bdi>{c.label}</bdi>
                </button>
              </li>
            ))}
            {!shown.length && <li className="muted">{t("relation.noResults")}</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
