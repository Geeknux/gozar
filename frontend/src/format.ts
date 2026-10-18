const faNumber = new Intl.NumberFormat("fa-IR");
const faDate = new Intl.DateTimeFormat("fa-IR-u-ca-persian", { dateStyle: "medium" });
const faDateTime = new Intl.DateTimeFormat("fa-IR-u-ca-persian", {
  dateStyle: "medium",
  timeStyle: "short",
});

const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";

export function faDigits(value: string | number): string {
  return String(value).replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)]);
}

export function toLatinDigits(value: string): string {
  return value
    .replace(/[۰-۹]/g, (d) => String(FA_DIGITS.indexOf(d)))
    .replace(/[٠-٩]/g, (d) => String("٠١٢٣٤٥٦٧٨٩".indexOf(d)));
}

export function formatNumber(value: string | number): string {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? faNumber.format(n) : String(value);
}

export function formatDate(iso: string): string {
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return iso.length === 10 ? faDate.format(d) : faDateTime.format(d);
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(T|$)/;

/** Render any API cell value for display (Jalali dates, Persian digits). */
export function formatCell(value: unknown, kind?: string): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "✔" : "✘";
  if (typeof value === "object") {
    if (Array.isArray(value)) return value.map((v) => formatCell(v)).join("، ");
    const ref = value as { label?: string; name?: string };
    return ref.label ?? ref.name ?? JSON.stringify(value);
  }
  if (typeof value === "number" || kind === "decimal" || kind === "integer" || kind === "float") {
    return formatNumber(value as number);
  }
  if (typeof value === "string" && (kind === "date" || kind === "datetime" || ISO_DATE.test(value))) {
    return formatDate(value);
  }
  return faDigits(String(value));
}
