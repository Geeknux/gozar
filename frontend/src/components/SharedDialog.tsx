import { type FormEvent, useState } from "react";

import { useI18n } from "../i18n";
import { errorText, type Sharing, type Texts } from "../sharing";
import Dialog from "./Dialog";
import ShareFields from "./ShareFields";
import TranslatedInput, { cleanTexts, hasText } from "./TranslatedInput";

/** What the dialog edits; ``name_i18n``/``description_i18n`` go to the API as they are. */
export type SharedValues = { name_i18n: Texts; description_i18n: Texts; sharing: Sharing };

type Props = {
  title: string;
  initial?: Partial<SharedValues>;
  submitLabel: string;
  onSubmit: (values: SharedValues) => Promise<unknown>;
  onClose: () => void;
};

const EMPTY: SharedValues = { name_i18n: {}, description_i18n: {}, sharing: { visibility: "private", users: [], groups: [] } };

/** Name, description (one field per language) and sharing of a saved report or
 * dashboard (create, rename, share). */
export default function SharedDialog({ title, initial, submitLabel, onSubmit, onClose }: Props) {
  const { t } = useI18n();
  const [values, setValues] = useState<SharedValues>({ ...EMPTY, ...initial });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSubmit({ ...values, name_i18n: cleanTexts(values.name_i18n), description_i18n: cleanTexts(values.description_i18n) });
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={title} onClose={onClose}>
      <form className="dialog-form" onSubmit={submit}>
        <TranslatedInput label={t("saved.name")} value={values.name_i18n} maxLength={200} autoFocus onChange={(name_i18n) => setValues({ ...values, name_i18n })} />
        <TranslatedInput
          label={t("saved.description")}
          value={values.description_i18n}
          multiline
          onChange={(description_i18n) => setValues({ ...values, description_i18n })}
        />
        <p className="muted small">{t("texts.hint")}</p>
        <ShareFields value={values.sharing} onChange={(sharing) => setValues({ ...values, sharing })} />
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || !hasText(values.name_i18n)}>
            {submitLabel}
          </button>
          <button type="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
