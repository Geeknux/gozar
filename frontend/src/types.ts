export type Perms = {
  view: boolean;
  add: boolean;
  change: boolean;
  delete: boolean;
  export?: boolean;
  import?: boolean;
};

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
  internal_type?: string;
  image?: boolean; // an ImageField (kind "file")
  allow_unicode?: boolean; // a SlugField that keeps non-ASCII letters
  computed?: boolean; // a read-only value computed by a callable
  boolean?: boolean; // a computed value shown as a yes/no icon
  form_only?: boolean; // a field of the add/password form, not of the model
  initial?: string | boolean | number | null;
};

export type Column = {
  name: string;
  label: string;
  kind: string;
  sortable: boolean;
  is_link: boolean;
  boolean?: boolean;
  empty_value?: string;
  editable?: boolean;
  image?: boolean;
};

export type WidgetType =
  | "input" | "textarea" | "select" | "radio" | "rich_select" | "checkbox" | "autocomplete"
  | "raw_id" | "dual_list" | "chips" | "permissions" | "password" | "file" | "image";
export type Widget = { type: WidgetType; orientation?: "horizontal" | "vertical" };

export type Fieldset = {
  title: string | null;
  description: string;
  classes: string[];
  collapse: boolean;
  rows: string[][];
};

export type InlineMeta = {
  name: string;
  model: string;
  style: "tabular" | "stacked";
  verbose_name: string;
  verbose_name_plural: string;
  fields: string[];
  readonly_fields: string[];
  field_meta: FieldMeta[];
  widgets: Record<string, Widget>;
  extra: number;
  min_num: number | null;
  max_num: number | null;
  can_delete: boolean;
  permissions: { view: boolean; add: boolean; change: boolean; delete: boolean };
  show_change_link: boolean;
  pk_name: string;
  defaults: Record<string, unknown>;
};

export type FileValue = { name: string; filename: string; url: string | null };

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
    available_columns: { name: string; label: string }[];
    search_fields: string[];
    search_help_text: string;
    filters: FilterSpec[];
    ordering: string[];
    per_page: number;
    editable: string[];
    bulk_edit_fields: string[];
    import_fields: string[];
    date_hierarchy: { field: string; label: string } | null;
    show_full_result_count: boolean;
    actions: ActionMeta[];
  };
  form: {
    fields: string[];
    readonly_fields: string[];
    password_fields: string[];
    fieldsets: Fieldset[];
    widgets: Record<string, Widget>;
    prepopulated: Record<string, string[]>;
    save_as: boolean;
    save_as_continue: boolean;
    save_on_top: boolean;
    change_password: boolean;
    password_help: string[];
    add: { fields: string[]; fieldsets: Fieldset[]; custom_form: boolean };
    inlines: InlineMeta[];
  };
};

export type Ref = { pk: string | number | null; label: string; model?: string; restricted?: boolean };

export type ListRow = {
  pk: string | number;
  label: string;
  cells: unknown[];
  edit?: Record<string, unknown>; // raw values of list_editable cells
};

export type DateHierarchy = {
  field: string;
  label: string;
  calendar: "gregorian" | "jalali";
  selected: { year: number | null; month: number | null; day: number | null };
  level: "year" | "month" | "day" | null;
  start: string | null; // first day of the selected year/month/day
  buckets: { year: number; month?: number; day?: number; start: string; count: number }[];
};

export type ListResponse = {
  columns: Column[];
  rows: ListRow[];
  count: number;
  total: number | null; // null with show_full_result_count = False
  page: number;
  page_size: number;
  num_pages: number;
  date_hierarchy?: DateHierarchy;
};

export type InlineRow = { pk: string | number; label: string; values: Record<string, unknown> };

export type ObjectData = {
  pk: string | number | null;
  label: string;
  values: Record<string, unknown>;
  hidden_links?: Record<string, number>; // linked rows outside the user's scope (kept on save)
  inlines?: Record<string, { rows: InlineRow[]; hidden: number }>;
  site_url?: string;
  password_status?: { usable: boolean; algorithm: string | null };
};

export type SavedView = {
  id: number;
  model: string;
  name: string;
  params: Record<string, string>;
  columns: string[];
  is_default: boolean;
  updated_at: string;
};

export type DeletePlan = {
  deletable: boolean;
  summary: { model: string; verbose_name: string; count: number }[];
  protected: { model: string; verbose_name: string; count: number; sample: Ref[] }[];
  perms_needed: string[];
  out_of_scope?: { model: string; verbose_name: string; count: number }[];
};

export type DiffEntry = { field: string; label: string; kind: string; old: unknown; new: unknown };

export type HistoryEntry = {
  timestamp: string;
  user: string | null;
  action: string;
  action_display: string;
  changes: { fields?: string[] };
  diff: DiffEntry[];
  source?: "list" | "bulk" | "import";
  inlines?: {
    name: string;
    verbose_name: string;
    added: string[];
    changed: { label: string; fields: string[]; diff: DiffEntry[] }[];
    deleted: string[];
  }[];
};

export type RecentAction = {
  timestamp: string;
  action: "add" | "change" | "delete";
  action_display: string;
  model: string;
  verbose_name: string;
  object_pk: string;
  object_repr: string;
  exists: boolean;
};

export type Preferences = {
  language: "en" | "fa";
  calendar: "gregorian" | "jalali";
  options: Record<string, unknown>;
  timezone: string;
  stored: { language: string; calendar: string };
  defaults: { language: "en" | "fa"; calendar: "gregorian" | "jalali"; timezone: string };
  available: {
    languages: { value: "en" | "fa"; label: string; dir: "ltr" | "rtl" }[];
    calendars: { value: "gregorian" | "jalali"; label: string }[];
  };
};
