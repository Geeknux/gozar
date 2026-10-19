import { type FormEvent, useState } from "react";

import { useI18n } from "../i18n";
import { errorText, type Sharing } from "../sharing";
import Dialog from "./Dialog";
import ShareFields from "./ShareFields";

export type SharedValues = { name: string; description: string; sharing: Sharing };

type Props = {
  title: string;
  initial?: Partial<SharedValues>;
  submitLabel: string;
  onSubmit: (values: SharedValues) => Promise<unknown>;
  onClose: () => void;
};

const EMPTY: SharedValues = { name: "", description: "", sharing: { visibility: "private", users: [], groups: [] } };

/** Name, description and sharing of a saved report or dashboard (create, rename, share). */
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
      await onSubmit(values);
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
        <label className="stack">
          {t("saved.name")}
          <input dir="auto" value={values.name} required maxLength={200} autoFocus onChange={(e) => setValues({ ...values, name: e.target.value })} />
        </label>
        <label className="stack">
          {t("saved.description")}
          <textarea dir="auto" rows={2} value={values.description} onChange={(e) => setValues({ ...values, description: e.target.value })} />
        </label>
        <ShareFields value={values.sharing} onChange={(sharing) => setValues({ ...values, sharing })} />
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={busy || !values.name.trim()}>
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
