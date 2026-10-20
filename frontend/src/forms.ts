/** Form state ⇄ API payload conversions shared by the change form, inlines, list
 * editing, bulk edit and the password page. */
import { isStoredFile, isUpload } from "./components/FileInput";
import { toLatinDigits } from "./i18n";
import type { FieldMeta, Ref } from "./types";

export type Values = Record<string, unknown>;
export type Errors = Record<string, string[]>;

const NUMERIC = new Set(["integer", "decimal", "float"]);

/** Convert one form value to the JSON the API expects (file fields: see filePayload). */
export function toPayload(field: FieldMeta, value: unknown): unknown {
  if (value === "" || value === undefined) {
    if (field.kind === "boolean") return false;
    if (field.kind === "m2m") return [];
    return field.null || NUMERIC.has(field.kind) || field.kind === "datetime" || field.kind === "date" ? null : "";
  }
  if (NUMERIC.has(field.kind) && typeof value === "string") return toLatinDigits(value).replace(/[,٬]/g, "").replace("٫", ".");
  if (field.kind === "datetime" && typeof value === "string" && !/[zZ]|[+-]\d\d:?\d\d$/.test(value)) {
    return new Date(value).toISOString();
  }
  if (field.kind === "fk" || field.kind === "o2o") return value ? (value as Ref).pk : null;
  if (field.kind === "m2m") return ((value as Ref[]) ?? []).map((r) => r.pk);
  if (field.kind === "json" && typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value;
}

export function fromApi(field: FieldMeta, value: unknown): unknown {
  if (field.kind === "json") return value === null || value === undefined ? "" : JSON.stringify(value, null, 2);
  if (field.kind === "file") return value ?? null;
  if (field.kind === "datetime") return value === null ? "" : value;
  return value ?? "";
}

/**
 * Put a file field's value into the request: a new upload becomes a multipart
 * part named ``part`` (and is left out of the JSON), a cleared file is ``null``, a
 * kept file is left out — or sent back as read when ``keep`` (save as new).
 */
export function filePayload(value: unknown, part: string, body: Values, name: string, files: Map<string, File>, keep = false) {
  if (isUpload(value)) files.set(part, value.upload);
  else if (value === null) body[name] = null;
  else if (keep && isStoredFile(value)) body[name] = value;
}

/** JSON, or multipart with the JSON in ``__payload__`` when there are uploads. */
export function requestBody(body: Values, files: Map<string, File>): Values | FormData {
  if (!files.size) return body;
  const data = new FormData();
  data.set("__payload__", JSON.stringify(body));
  for (const [part, file] of files) data.set(part, file, file.name);
  return data;
}

/** Django's slugify (admin's URLify) for prepopulated fields. */
export function slugify(text: string, allowUnicode: boolean): string {
  let s = text.normalize(allowUnicode ? "NFKC" : "NFKD");
  s = allowUnicode ? s.replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, "") : s.replace(/[̀-ͯ]/g, "").replace(/[^\w\s-]/g, "");
  return s
    .replace(/[‌‍]/g, "-")
    .trim()
    .toLocaleLowerCase()
    .replace(/[-\s]+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "");
}

export function errorList(errors: Errors | undefined, key: string): string[] {
  return errors?.[key]?.filter(Boolean) ?? [];
}
