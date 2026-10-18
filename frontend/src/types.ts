export type Perms = { view: boolean; add: boolean; change: boolean; delete: boolean };

export type ModelEntry = {
  label: string;
  model_name: string;
  verbose_name: string;
  verbose_name_plural: string;
  permissions: Perms;
};

export type AppEntry = { app_label: string; verbose_name: string; models: ModelEntry[] };

export type Choice = { value: string | number | boolean; label: string };

export type FieldKind =
  | "string" | "text" | "integer" | "decimal" | "float" | "boolean" | "date" | "datetime"
  | "time" | "duration" | "uuid" | "json" | "email" | "url" | "file" | "fk" | "o2o" | "m2m"
  | "password" | "other";

export type FieldMeta = {
  name: string;
  verbose_name: string;
  kind: FieldKind;
  primary_key: boolean;
  editable: boolean;
  null: boolean;
  blank: boolean;
  required: boolean;
  max_length: number | null;
  decimal_places: number | null;
  help_text: string;
  choices: Choice[] | null;
  related_model: string | null;
};

export type Column = { name: string; label: string; kind: string; sortable: boolean; is_link: boolean };

export type FilterSpec = {
  name: string;
  label: string;
  kind: string;
  null: boolean;
  choices?: Choice[];
  related_model?: string;
};

export type ActionMeta = { name: string; label: string; destructive: boolean; allowed: boolean };

export type ModelMeta = {
  label: string;
  app_label: string;
  model_name: string;
  verbose_name: string;
  verbose_name_plural: string;
  app_verbose_name: string;
  pk_name: string;
  fields: FieldMeta[];
  permissions: Perms;
  list: {
    columns: Column[];
    search_fields: string[];
    filters: FilterSpec[];
    ordering: string[];
    per_page: number;
    actions: ActionMeta[];
  };
  form: { fields: string[]; readonly_fields: string[]; password_fields: string[] };
};

export type Ref = { pk: string | number; label: string; model?: string };

export type ListResponse = {
  columns: Column[];
  rows: { pk: string | number; label: string; cells: unknown[] }[];
  count: number;
  total: number;
  page: number;
  page_size: number;
  num_pages: number;
};

export type ObjectData = { pk: string | number | null; label: string; values: Record<string, unknown> };

export type DeletePlan = {
  deletable: boolean;
  summary: { model: string; verbose_name: string; count: number }[];
  protected: { model: string; verbose_name: string; count: number; sample: Ref[] }[];
  perms_needed: string[];
};

export type HistoryEntry = {
  timestamp: string;
  user: string | null;
  action: string;
  action_display: string;
  changes: { fields?: string[] };
};

export type Preferences = {
  language: "en" | "fa";
  calendar: "gregorian" | "jalali";
  options: Record<string, unknown>;
  stored: { language: string; calendar: string };
  defaults: { language: "en" | "fa"; calendar: "gregorian" | "jalali" };
  available: {
    languages: { value: "en" | "fa"; label: string; dir: "ltr" | "rtl" }[];
    calendars: { value: "gregorian" | "jalali"; label: string }[];
  };
};
