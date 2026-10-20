import { useEffect, useMemo } from "react";

import { useI18n } from "../i18n";
import type { FileValue } from "../types";

/** A file field's form value: the stored file, a new upload, or null (none / cleared). */
export type FileState = FileValue | { upload: File } | null;

export const isUpload = (v: unknown): v is { upload: File } =>
  !!v && typeof v === "object" && "upload" in (v as object) && (v as { upload: unknown }).upload instanceof File;

export const isStoredFile = (v: unknown): v is FileValue =>
  !!v && typeof v === "object" && "name" in (v as object) && !("upload" in (v as object));

const IMAGE_NAME = /\.(png|jpe?g|gif|webp|avif|svg|bmp)$/i;

type Props = {
  id?: string;
  value: FileState;
  initial: FileValue | null; // what is stored now (to undo a clear or a new upload)
  onChange: (value: FileState) => void;
  image?: boolean;
  required?: boolean;
  compact?: boolean;
};

/** Upload, preview, keep or clear a file (admin's ClearableFileInput). */
export default function FileInput({ id, value, initial, onChange, image, required, compact }: Props) {
  const { t } = useI18n();
  const upload = isUpload(value) ? value.upload : null;
  const preview = useMemo(() => (upload && image ? URL.createObjectURL(upload) : null), [upload, image]);
  useEffect(() => () => (preview ? URL.revokeObjectURL(preview) : undefined), [preview]);
  const stored = isStoredFile(value) ? value : null;
  const showImage = image || (stored && IMAGE_NAME.test(stored.name));

  return (
    <div className={`file-input${compact ? " compact" : ""}`}>
      {stored && (
        <div className="file-current">
          {showImage && stored.url && (
            <a href={stored.url} target="_blank" rel="noreferrer">
              <img className="thumb" src={stored.url} alt={stored.filename} />
            </a>
          )}
          <span>
            {!compact && <span className="muted">{t("file.current")} </span>}
            {stored.url ? (
              <a href={stored.url} target="_blank" rel="noreferrer" dir="auto">
                {stored.filename}
              </a>
            ) : (
              <bdi>{stored.filename}</bdi>
            )}
          </span>
          {!required && (
            <button type="button" className="link danger-link" onClick={() => onChange(null)}>
              {t("file.clear")}
            </button>
          )}
        </div>
      )}
      {!stored && initial && !upload && (
        <div className="file-current muted">
          {t("file.cleared")}{" "}
          <button type="button" className="link" onClick={() => onChange(initial)}>
            {t("file.undo")}
          </button>
        </div>
      )}
      {upload && (
        <div className="file-current">
          {preview && <img className="thumb" src={preview} alt={upload.name} />}
          <span>
            <span className="muted">{t("file.new")} </span>
            <bdi>{upload.name}</bdi>
          </span>
          <button type="button" className="link" onClick={() => onChange(initial)}>
            {t("file.undo")}
          </button>
        </div>
      )}
      <input
        id={id}
        type="file"
        accept={image ? "image/*" : undefined}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onChange({ upload: file });
          e.target.value = "";
        }}
      />
    </div>
  );
}
