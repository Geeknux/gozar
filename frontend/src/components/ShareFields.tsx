import { type MessageKey, useI18n } from "../i18n";
import type { Sharing, Visibility } from "../sharing";
import PeoplePicker from "./PeoplePicker";

const VISIBILITIES: Visibility[] = ["private", "users", "everyone"];

/** Who can open a saved report or dashboard. Viewers always see their own data. */
export default function ShareFields({ value, onChange }: { value: Sharing; onChange: (s: Sharing) => void }) {
  const { t } = useI18n();
  return (
    <fieldset className="share-fields">
      <legend>{t("share.title")}</legend>
      {VISIBILITIES.map((v) => (
        <label key={v} className="choice">
          <input type="radio" name="visibility" checked={value.visibility === v} onChange={() => onChange({ ...value, visibility: v })} />
          {t(`share.${v}` as MessageKey)}
        </label>
      ))}
      {value.visibility === "users" && (
        <PeoplePicker users={value.users} groups={value.groups} onChange={(users, groups) => onChange({ ...value, users, groups })} />
      )}
      <p className="muted small">{t("share.scopeNote")}</p>
    </fieldset>
  );
}
