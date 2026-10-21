import { useI18n } from "../i18n";
import type { FieldMeta, FileValue, Ref, Widget } from "../types";
import ChoiceSelect from "./ChoiceSelect";
import DatePicker, { DateTimePicker } from "./DatePicker";
import DualList from "./DualList";
import FileInput, { type FileState } from "./FileInput";
import PermissionTree from "./PermissionTree";
import RawIdInput from "./RawIdInput";
import RelationSelect from "./RelationSelect";
import { isolate } from "../bidi";

type Props = {
  field: FieldMeta;
  widget?: Widget;
  model: string; // label of the model the form edits (the autocomplete endpoint's)
  inline?: string; // the inline the field belongs to
  value: unknown;
  initial?: unknown; // the stored value (file fields: to undo a clear)
  onChange: (v: unknown) => void;
  isNew?: boolean;
  id?: string;
  compact?: boolean; // table cells (tabular inlines, list editing)
};

/** The editor of one form field, by its widget (ModelOptions.widgets & co.). */
export default function FieldInput({ field, widget, model, inline, value, initial, onChange, isNew, id, compact }: Props) {
  const { t } = useI18n();
  const str = value === null || value === undefined ? "" : String(value);
  const type = widget?.type ?? (field.choices ? "select" : undefined);

  if (field.kind === "file" || type === "file" || type === "image") {
    return (
      <FileInput
        id={id}
        value={(value as FileState) ?? null}
        initial={(initial as FileValue | null) ?? null}
        onChange={onChange}
        image={field.image || type === "image"}
        required={field.required}
        compact={compact}
      />
    );
  }

  if (field.choices && type === "radio") {
    return (
      <div id={id} role="radiogroup" className={`radios ${widget?.orientation === "horizontal" ? "horizontal" : ""}`}>
        {(field.blank || field.null) && !field.form_only && (
          <label className="choice">
            <input type="radio" checked={str === ""} onChange={() => onChange("")} /> {t("common.none")}
          </label>
        )}
        {field.choices.map((c) => (
          <label key={String(c.value)} className="choice">
            <input type="radio" checked={str === String(c.value)} onChange={() => onChange(String(c.value))} />
            <bdi>{c.label}</bdi>
          </label>
        ))}
      </div>
    );
  }

  if (field.choices && type === "rich_select") {
    return <ChoiceSelect id={id} choices={field.choices} value={str} onChange={onChange} allowEmpty={field.blank || field.null} />;
  }

  if (field.choices) {
    return (
      <select id={id} value={str} onChange={(e) => onChange(e.target.value)}>
        {(field.blank || field.null || !str) && <option value="">---------</option>}
        {field.choices.map((c) => (
          <option key={String(c.value)} value={String(c.value)}>
            {isolate(c.label)}
          </option>
        ))}
      </select>
    );
  }

  switch (field.kind) {
    case "boolean":
      if (field.null) {
        return (
          <select id={id} value={str} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "true")}>
            <option value="">{t("common.unknown")}</option>
            <option value="true">{t("common.yes")}</option>
            <option value="false">{t("common.no")}</option>
          </select>
        );
      }
      return <input id={id} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />;
    case "text":
      if (type === "input") return <input id={id} dir="auto" value={str} onChange={(e) => onChange(e.target.value)} />;
      return <textarea id={id} dir="auto" rows={compact ? 2 : 4} value={str} onChange={(e) => onChange(e.target.value)} />;
    case "json":
      return <textarea id={id} dir="ltr" rows={6} className="mono" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "integer":
    case "decimal":
    case "float":
      return <input id={id} dir="ltr" inputMode="decimal" className={compact ? "num-input" : ""} value={str} onChange={(e) => onChange(e.target.value)} />;
    case "date":
      return <DatePicker id={id} value={str.slice(0, 10)} onChange={onChange} clearable={field.null} />;
    case "datetime":
      return <DateTimePicker id={id} value={str} onChange={onChange} />;
    case "time":
      return <input id={id} type="time" value={str.slice(0, 5)} onChange={(e) => onChange(e.target.value)} />;
    case "email":
      return <input id={id} type="email" dir="ltr" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "url":
      return <input id={id} type="url" dir="ltr" value={str} onChange={(e) => onChange(e.target.value)} />;
    case "password":
      return (
        <input
          id={id}
          type="password"
          dir="ltr"
          autoComplete="new-password"
          placeholder={isNew || field.form_only ? "" : t("form.passwordPlaceholder")}
          value={str}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "fk":
    case "o2o": {
      const ref = value && typeof value === "object" ? (value as Ref) : null;
      if (type === "raw_id") {
        return <RawIdInput id={id} model={model} field={field.name} inline={inline} relatedModel={field.related_model} value={ref} onChange={onChange} />;
      }
      return <RelationSelect model={model} field={field.name} inline={inline} value={ref} onChange={onChange} />;
    }
    case "m2m": {
      const refs = Array.isArray(value) ? (value as Ref[]) : [];
      if (type === "permissions") return <PermissionTree model={model} field={field.name} value={refs} onChange={onChange} />;
      if (type === "dual_list") {
        return (
          <DualList
            model={model}
            field={field.name}
            inline={inline}
            value={refs}
            onChange={onChange}
            vertical={widget?.orientation === "vertical"}
            label={field.verbose_name}
          />
        );
      }
      return (
        <div className="m2m">
          <ul className="chips">
            {refs.map((r) => (
              <li key={String(r.pk)}>
                <bdi>{r.label}</bdi>
                <button type="button" aria-label={t("common.remove")} onClick={() => onChange(refs.filter((x) => x.pk !== r.pk))}>
                  ×
                </button>
              </li>
            ))}
          </ul>
          <RelationSelect
            model={model}
            field={field.name}
            inline={inline}
            value={null}
            placeholder={t("form.addItem")}
            onChange={(r) => r && !refs.some((x) => x.pk === r.pk) && onChange([...refs, r])}
          />
        </div>
      );
    }
    default:
      if (type === "textarea") return <textarea id={id} dir="auto" rows={4} value={str} onChange={(e) => onChange(e.target.value)} />;
      return <input id={id} dir="auto" maxLength={field.max_length ?? undefined} value={str} onChange={(e) => onChange(e.target.value)} />;
  }
}
