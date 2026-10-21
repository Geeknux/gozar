/** Dashboards (bi_admin.api.dashboards). */
import { api } from "../api";
import type { ChartType, Choice, Spec } from "../reports/model";
import type { Shared, SharedTextsIn, SharingIn, Texts } from "../sharing";

export type Layout = { x: number; y: number; w: number; h: number };

export type Widget = {
  id: number;
  /** the widget's title, else its saved report's name (in the viewer's language) */
  title: string;
  /** the widget's own title per language (empty: it shows the saved report's name) */
  title_i18n: Texts;
  saved_report: { id: number; name: string } | null;
  inline: boolean;
  spec: Spec | null;
  chart: ChartType;
  problem: string;
  layout: Layout;
};

/** Global filters: a period or a range, and category values per dimension key. */
export type Filters = {
  period?: string | null;
  range?: [string, string] | null;
  dimensions?: string[];
  values?: Record<string, (string | number | boolean)[]>;
};

export type FilterOption = {
  key: string;
  label: string;
  type: string;
  semantic_model: string;
  choices: Choice[] | null;
};

export type DashboardSummary = Shared & { widget_count: number | null; is_home: boolean };

export type Dashboard = DashboardSummary & {
  widgets: Widget[];
  filters: Filters;
  filter_options: FilterOption[];
};

export type ResolvedWidget = Widget & { applied: string[]; ignored: string[] };

/** What PATCH/POST accept for a widget. */
export type WidgetIn = {
  title_i18n: Texts;
  saved_report: number | null;
  spec: Spec | null;
  chart: string;
} & Layout;

export const widgetIn = (w: Widget): WidgetIn => ({
  title_i18n: w.title_i18n,
  saved_report: w.saved_report?.id ?? null,
  spec: w.saved_report ? null : w.spec,
  chart: w.chart,
  ...w.layout,
});

export const listDashboards = () =>
  api<{ dashboards: DashboardSummary[]; home: number | null }>("/dashboards");
export const getDashboard = (id: number | string) => api<Dashboard>(`/dashboards/${id}`);
export const createDashboard = (body: SharedTextsIn & { sharing: SharingIn; widgets?: WidgetIn[]; filters?: Filters }) =>
  api<Dashboard>("/dashboards", { method: "POST", body });
export const updateDashboard = (
  id: number,
  body: Partial<SharedTextsIn & { sharing: SharingIn; widgets: WidgetIn[]; filters: Filters }>,
) => api<Dashboard>(`/dashboards/${id}`, { method: "PATCH", body });
export const deleteDashboard = (id: number) => api(`/dashboards/${id}`, { method: "DELETE" });
export const duplicateDashboard = (id: number) => api<Dashboard>(`/dashboards/${id}/duplicate`, { method: "POST" });
export const resolveDashboard = (id: number, filters: Filters) =>
  api<{ filters: Filters; widgets: ResolvedWidget[] }>(`/dashboards/${id}/resolve`, { method: "POST", body: { filters } });

/** The first free row below every widget. */
export const nextRow = (widgets: { layout: Layout }[]) => widgets.reduce((y, w) => Math.max(y, w.layout.y + w.layout.h), 0);
