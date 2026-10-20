import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, crudPath } from "../api";
import { toLatinDigits, useI18n } from "../i18n";
import type { Ref } from "../types";
import RelationSelect from "./RelationSelect";

type Props = {
  id?: string;
  model: string;
  field: string;
  inline?: string;
  relatedModel: string | null;
  value: Ref | null;
  onChange: (value: Ref | null) => void;
};

/**
 * admin's raw_id_fields: the related row's id typed in directly, its label shown
 * next to it, and a lookup button that opens the search picker.
 */
export default function RawIdInput({ id, model, field, inline, relatedModel, value, onChange }: Props) {
  const { t } = useI18n();
  const [text, setText] = useState(value ? String(value.pk ?? "") : "");
  const [lookup, setLookup] = useState(false);
  const [missing, setMissing] = useState(false);

  useEffect(() => setText(value?.pk !== undefined && value?.pk !== null ? String(value.pk) : ""), [value]);

  async function resolve(raw: string) {
    const pk = toLatinDigits(raw.trim());
    setMissing(false);
    if (!pk) return onChange(null);
    if (value && String(value.pk) === pk) return;
    try {
      // the autocomplete endpoint only returns rows the user may pick
      const r = await api<{ results: Ref[] }>(crudPath(model, "autocomplete"), {
        params: { field, pk, ...(inline ? { inline } : {}) },
      });
      if (r.results.length) onChange({ ...r.results[0], model: relatedModel ?? undefined });
      else setMissing(true);
    } catch {
      setMissing(true);
    }
  }

  return (
    <div className="raw-id">
      <div className="inline-row">
        <input
          id={id}
          dir="ltr"
          inputMode="numeric"
          className="raw-id-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => resolve(text)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              resolve(text);
            }
          }}
        />
        <button type="button" title={t("widget.lookup")} aria-label={t("widget.lookup")} onClick={() => setLookup((o) => !o)}>
          🔍
        </button>
        {value && !value.restricted && relatedModel && value.pk !== null && (
          <Link to={`/${relatedModel.replace(".", "/")}/${value.pk}`}>
            <bdi>{value.label}</bdi>
          </Link>
        )}
        {value?.restricted && <span className="muted">{value.label}</span>}
      </div>
      {missing && <small className="field-error">{t("widget.notFound")}</small>}
      {lookup && (
        <RelationSelect
          model={model}
          field={field}
          inline={inline}
          value={value}
          onChange={(r) => {
            onChange(r ? { ...r, model: relatedModel ?? undefined } : null);
            setLookup(false);
          }}
        />
      )}
    </div>
  );
}
