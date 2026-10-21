import { useI18n } from "../i18n";
import { useSession } from "../session";
import type { Texts } from "../sharing";

type Props = {
  label: string;
  value: Texts;
  onChange: (value: Texts) => void;
  multiline?: boolean;
  maxLength?: number;
  autoFocus?: boolean;
};

/** One field per configured language (the default language first) for a name, title
 * or description that Gozar stores; viewers get their language's text, else the
 * default's. With a single language it is a plain field. */
export default function TranslatedInput({ label, value, onChange, multiline = false, maxLength, autoFocus = false }: Props) {
  const { t, lang } = useI18n();
  const { content } = useSession();
  const languages = [...content.languages].sort(
    (a, b) => Number(b.value === content.defaultLanguage) - Number(a.value === content.defaultLanguage),
  );
  const field = (code: string, labelled: boolean) => {
    const props = {
      dir: "auto" as const,
      lang: code,
      value: value[code as keyof Texts] ?? "",
      maxLength,
      autoFocus: autoFocus && (code === lang || !labelled),
      onChange: (e: { target: { value: string } }) => onChange({ ...value, [code]: e.target.value }),
    };
    return multiline ? <textarea rows={2} {...props} /> : <input {...props} />;
  };

  if (languages.length <= 1) {
    return (
      <label className="stack">
        {label}
        {field(content.defaultLanguage, false)}
      </label>
    );
  }
  return (
    <fieldset className="translated stack">
      <legend>{label}</legend>
      {languages.map((l) => (
        <label key={l.value} className="translated-field">
          <span className="muted small">
            <bdi>{l.label}</bdi>
            {l.value === content.defaultLanguage && ` · ${t("texts.defaultLanguage")}`}
          </span>
          {field(l.value, true)}
        </label>
      ))}
    </fieldset>
  );
}

/** Texts with surrounding spaces removed and empty languages dropped (what the API stores). */
export const cleanTexts = (value: Texts): Texts =>
  Object.fromEntries(Object.entries(value).map(([k, v]) => [k, (v ?? "").trim()]).filter(([, v]) => v));

export const hasText = (value: Texts) => Object.values(value).some((v) => v && v.trim());
