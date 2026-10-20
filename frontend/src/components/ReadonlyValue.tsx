import { Link } from "react-router-dom";

import { useI18n } from "../i18n";
import type { FieldMeta, FileValue, Ref } from "../types";
import { isStoredFile } from "./FileInput";

const IMAGE_NAME = /\.(png|jpe?g|gif|webp|avif|svg|bmp)$/i;

/** A read-only form value: booleans as icons, files as links or thumbnails,
 * related rows as links (never for restricted ones). */
export default function ReadonlyValue({ field, value, empty }: { field?: FieldMeta; value: unknown; empty?: string }) {
  const { t, fmt } = useI18n();
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) {
    return <span className="readonly muted">{empty ?? "—"}</span>;
  }
  if (typeof value === "boolean" || field?.boolean) {
    return <BooleanIcon value={Boolean(value)} label={value ? t("common.yes") : t("common.no")} />;
  }
  if (isStoredFile(value)) return <FileLink value={value} image={field?.image} />;
  const ref = value && typeof value === "object" && "pk" in (value as object) ? (value as Ref) : null;
  if (ref?.model && !ref.restricted && ref.pk !== null) {
    return (
      <span className="readonly">
        <Link to={`/${ref.model.replace(".", "/")}/${ref.pk}`}>
          <bdi>{ref.label}</bdi>
        </Link>
      </span>
    );
  }
  return (
    <span className="readonly">
      <bdi>{fmt.cell(value, field?.kind)}</bdi>
    </span>
  );
}

export function BooleanIcon({ value, label }: { value: boolean | null; label?: string }) {
  const { t } = useI18n();
  if (value === null) return <span className="bool-icon unknown" title={t("common.unknown")}>?</span>;
  return (
    <span className={`bool-icon ${value ? "yes" : "no"}`} role="img" aria-label={label ?? (value ? t("common.yes") : t("common.no"))}>
      {value ? "✔" : "✘"}
    </span>
  );
}

export function FileLink({ value, image }: { value: FileValue; image?: boolean }) {
  const showImage = image || IMAGE_NAME.test(value.name);
  if (!value.url) return <bdi>{value.filename}</bdi>;
  return (
    <a href={value.url} target="_blank" rel="noreferrer" className="file-link">
      {showImage ? <img className="thumb small" src={value.url} alt={value.filename} /> : <bdi dir="auto">{value.filename}</bdi>}
    </a>
  );
}
