import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import ReactGridLayout, { type Layout as GridLayout, useContainerWidth } from "react-grid-layout";
import { Link, useNavigate } from "react-router-dom";

import { UserText } from "../bidi";
import SharedDialog from "../components/SharedDialog";
import { type MessageKey, useI18n } from "../i18n";
import ScheduleDialog from "../schedules/ScheduleDialog";
import { homeDashboard, useSession } from "../session";
import { errorText, personName, sharingIn } from "../sharing";
import {
  type Dashboard,
  deleteDashboard,
  duplicateDashboard,
  type Filters,
  getDashboard,
  nextRow,
  type ResolvedWidget,
  resolveDashboard,
  updateDashboard,
  type Widget,
  widgetIn,
} from "./api";
import FilterBar from "./FilterBar";
import WidgetDialog from "./WidgetDialog";
import WidgetView from "./WidgetView";

const COLS = 12;
const ROW_HEIGHT = 72;
const NARROW = 720; // below this width the grid becomes a single column

type Props = { id: number; startEditing?: boolean; onMissing?: () => void; titleLevel?: "h1" | "h2" };

/** Viewer filters start from the dashboard's stored defaults. */
const viewerFilters = (board: Dashboard): Filters => ({
  period: board.filters.period ?? null,
  range: board.filters.range ?? null,
  values: board.filters.values ?? {},
});

/** A dashboard: global filters, a grid of widgets that load in parallel, and (for its
 * owner) an editor to add, remove, resize and reorder widgets by drag and drop. */
export default function DashboardView({ id, startEditing = false, onMissing, titleLevel = "h1" }: Props) {
  const { t, dir } = useI18n();
  const session = useSession();
  const navigate = useNavigate();
  const [board, setBoard] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState<Filters>({});
  const [resolved, setResolved] = useState<ResolvedWidget[] | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Widget[]>([]);
  const [offered, setOffered] = useState<string[]>([]);
  const [dialog, setDialog] = useState<null | "share" | "schedule" | "add" | { edit: Widget }>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const b = await getDashboard(id);
      setBoard(b);
      setFilters(viewerFilters(b));
      setOffered(b.filters.dimensions ?? []);
      return b;
    } catch (err) {
      setError(errorText(err));
      onMissing?.();
      return null;
    }
  }, [id, onMissing]);

  useEffect(() => {
    void load().then((b) => {
      if (b && startEditing && b.can_edit) {
        setDraft(b.widgets);
        setEditing(true);
      }
    });
  }, [load, startEditing]);

  // Every filter change resolves each widget's effective spec on the server.
  const filterKey = JSON.stringify(filters);
  useEffect(() => {
    if (!board || editing) return;
    let current = true;
    const clean: Filters = { ...filters, range: filters.range?.every(Boolean) ? filters.range : null };
    resolveDashboard(board.id, clean)
      .then((r) => current && setResolved(r.widgets))
      .catch((err) => current && setError(errorText(err)));
    return () => {
      current = false;
    };
  }, [board, filterKey, editing]);

  const widgets: Widget[] = editing ? draft : (resolved ?? []);
  const layout: GridLayout = useMemo(
    () =>
      widgets.map((w) => ({
        i: String(w.id),
        // the grid lays out left to right: mirror it for right-to-left pages
        x: dir === "rtl" ? COLS - w.layout.x - w.layout.w : w.layout.x,
        y: w.layout.y,
        w: w.layout.w,
        h: w.layout.h,
        minW: 2,
        minH: 2,
      })),
    [widgets, dir],
  );

  const onLayout = (next: GridLayout) => {
    if (!editing) return;
    const byId = new Map(next.map((item) => [item.i, item]));
    setDraft((all) =>
      all.map((w) => {
        const item = byId.get(String(w.id));
        if (!item) return w;
        const x = dir === "rtl" ? COLS - item.x - item.w : item.x;
        return { ...w, layout: { x, y: item.y, w: item.w, h: item.h } };
      }),
    );
  };

  if (error && !board) return <p className="error">{error}</p>;
  if (!board) return <p className="muted">{t("app.loading")}</p>;

  const home = homeDashboard(session) === board.id;
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = () => {
    setDraft(board.widgets);
    setOffered(board.filters.dimensions ?? []);
    setFilters(viewerFilters(board)); // the editor edits the stored defaults, not a viewer's choice
    setEditing(true);
  };
  const saveEdit = () =>
    act(async () => {
      const range = filters.range?.every(Boolean) ? filters.range : null;
      const next = await updateDashboard(board.id, {
        widgets: draft.map(widgetIn),
        filters: { period: filters.period ?? null, range, values: filters.values ?? {}, dimensions: offered },
      });
      setBoard(next);
      setFilters(viewerFilters(next));
      setEditing(false);
    });
  const cancelEdit = () => {
    setEditing(false);
    setFilters(viewerFilters(board));
  };
  let tempId = -1;
  const addWidget = (w: Pick<Widget, "title" | "title_i18n" | "saved_report" | "spec" | "chart" | "inline">) => {
    tempId = Math.min(-1, ...draft.map((d) => d.id)) - 1;
    const kpi = w.chart === "kpi";
    setDraft((all) => [
      ...all,
      { ...w, id: tempId, problem: "", layout: { x: 0, y: nextRow(all), w: kpi ? COLS : 6, h: kpi ? 2 : 4 } },
    ]);
  };

  const Title = titleLevel;
  const tiles = editing
    ? draft.map((w) => ({
        id: w.id,
        node: <EditCard widget={w} onEdit={() => setDialog({ edit: w })} onRemove={() => setDraft((all) => all.filter((x) => x.id !== w.id))} />,
      }))
    : (resolved ?? []).map((w) => ({ id: w.id, node: <WidgetView widget={w} options={board.filter_options} refresh={refresh} /> }));
  const grid: ReactNode = widgets.length ? (
    <Grid layout={layout} widgets={widgets} tiles={tiles} editing={editing} onLayout={onLayout} />
  ) : (
    <p className="empty">{editing ? t("dash.emptyEditing") : board.can_edit ? t("dash.emptyOwner") : t("dash.emptyViewer")}</p>
  );

  return (
    <div className={`dashboard${editing ? " editing" : ""}`}>
      <div className="page-head">
        <div>
          <Title>
            <bdi>{board.name}</bdi>
            {home && <span className="badge">{t("dash.home")}</span>}
          </Title>
          {board.description && <UserText className="dashboard-description">{board.description}</UserText>}
          <p className="muted small">
            {board.is_owner ? t("saved.mine") : t("saved.sharedBy", { name: personName(board.owner) })} ·{" "}
            {t(`share.short.${board.sharing.visibility}` as MessageKey)}
            {titleLevel === "h2" && (
              <>
                {" · "}
                <Link to={`/dashboards/${board.id}`}>{t("dash.openFull")}</Link>
              </>
            )}
          </p>
        </div>
        <div className="results-actions">
          {editing ? (
            <>
              <button type="button" onClick={() => setDialog("add")}>
                {t("dash.addWidget")}
              </button>
              <button type="button" className="primary" disabled={busy} onClick={saveEdit}>
                {t("dash.doneEditing")}
              </button>
              <button type="button" onClick={cancelEdit}>
                {t("common.cancel")}
              </button>
            </>
          ) : (
            <>
              <button type="button" title={t("report.refreshHint")} onClick={() => setRefresh((n) => n + 1)}>
                {t("report.run")}
              </button>
              {board.can_edit && (
                <button type="button" onClick={startEdit}>
                  {t("dash.edit")}
                </button>
              )}
              {board.can_edit && (
                <button type="button" onClick={() => setDialog("share")}>
                  {t("saved.renameShare")}
                </button>
              )}
              <button type="button" disabled={busy} onClick={() => act(() => session.setOption("home_dashboard", home ? null : board.id))}>
                {home ? t("dash.unpin") : t("dash.pin")}
              </button>
              <button type="button" disabled={busy} onClick={() => act(async () => navigate(`/dashboards/${(await duplicateDashboard(board.id)).id}`))}>
                {t("saved.duplicate")}
              </button>
              <button type="button" onClick={() => setDialog("schedule")}>
                {t("schedule.emailIt")}
              </button>
              {board.can_edit && (
                <button
                  type="button"
                  className="danger"
                  disabled={busy}
                  onClick={() =>
                    window.confirm(t("dash.confirmDelete", { name: board.name })) &&
                    act(async () => {
                      await deleteDashboard(board.id);
                      if (home) await session.setOption("home_dashboard", null);
                      navigate("/dashboards");
                    })
                  }
                >
                  {t("common.delete")}
                </button>
              )}
            </>
          )}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {editing && (
        <fieldset className="card filter-config">
          <legend>{t("dash.offeredFilters")}</legend>
          {board.filter_options.length === 0 && <span className="muted">{t("dash.noFilterOptions")}</span>}
          {board.filter_options.map((o) => (
            <label key={o.key} className="check-row">
              <input
                type="checkbox"
                checked={offered.includes(o.key)}
                onChange={(e) => setOffered(e.target.checked ? [...offered, o.key] : offered.filter((k) => k !== o.key))}
              />
              <bdi>{o.label}</bdi>
            </label>
          ))}
          <span className="muted small">{t("dash.defaultsNote")}</span>
        </fieldset>
      )}
      <FilterBar value={filters} dimensions={offered} options={board.filter_options} onChange={setFilters} />
      {grid}
      {dialog === "share" && (
        <SharedDialog
          title={t("dash.settings")}
          submitLabel={t("form.save")}
          initial={{ name_i18n: board.name_i18n, description_i18n: board.description_i18n, sharing: board.sharing }}
          onSubmit={async (v) => setBoard(await updateDashboard(board.id, { ...v, sharing: sharingIn(v.sharing) }))}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === "schedule" && (
        <ScheduleDialog
          target={{ kind: "dashboard", id: board.id, name: board.name }}
          me={session.user}
          defaultTimezone={session.timezone}
          onSaved={() => undefined}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === "add" && <WidgetDialog onDone={addWidget} onClose={() => setDialog(null)} />}
      {dialog && typeof dialog === "object" && (
        <WidgetDialog
          widget={dialog.edit}
          onDone={(w) => setDraft((all) => all.map((x) => (x.id === dialog.edit.id ? { ...x, ...w } : x)))}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}

type GridProps = {
  layout: GridLayout;
  widgets: Widget[];
  tiles: { id: number; node: ReactNode }[];
  editing: boolean;
  onLayout: (layout: GridLayout) => void;
};

/** The widget grid; measures its own width (it mounts once the dashboard is loaded).
 * Narrow screens get one column in reading order instead. */
function Grid({ layout, widgets, tiles, editing, onLayout }: GridProps) {
  const { width, containerRef, mounted } = useContainerWidth();
  let body: ReactNode = null;
  if (mounted && width < NARROW && !editing) {
    const order = [...widgets].sort((a, b) => a.layout.y - b.layout.y || a.layout.x - b.layout.x).map((w) => w.id);
    body = (
      <div className="widget-stack">
        {order.map((id) => {
          const w = widgets.find((x) => x.id === id)!;
          return (
            <div key={id} style={{ height: Math.max(w.layout.h, 3) * ROW_HEIGHT }}>
              {tiles.find((tile) => tile.id === id)?.node}
            </div>
          );
        })}
      </div>
    );
  } else if (mounted) {
    body = (
      <ReactGridLayout
        layout={layout}
        width={width}
        gridConfig={{ cols: COLS, rowHeight: ROW_HEIGHT, margin: [12, 12], containerPadding: [0, 0] }}
        dragConfig={{ enabled: editing, handle: ".drag-handle" }}
        resizeConfig={{ enabled: editing }}
        onLayoutChange={onLayout}
      >
        {tiles.map((tile) => (
          <div key={String(tile.id)}>{tile.node}</div>
        ))}
      </ReactGridLayout>
    );
  }
  return (
    <div ref={containerRef} className="dashboard-grid">
      {body}
    </div>
  );
}

function EditCard({ widget, onEdit, onRemove }: { widget: Widget; onEdit: () => void; onRemove: () => void }) {
  const { t } = useI18n();
  return (
    <section className="widget card edit-card">
      <header className="widget-head">
        <span className="drag-handle" title={t("dash.dragHint")} aria-label={t("dash.dragHint")}>
          ⠿
        </span>
        <h3 dir="auto">{widget.title || widget.saved_report?.name || t("dash.untitled")}</h3>
      </header>
      <p className="muted small">
        {t(`chart.${widget.chart}` as MessageKey)} ·{" "}
        {widget.saved_report ? (
          <>
            {t("dash.savedReport")}: <bdi>{widget.saved_report.name}</bdi>
          </>
        ) : (
          t("dash.inlineReport")
        )}
      </p>
      {widget.problem && <p className="muted small"><bdi>{widget.problem}</bdi></p>}
      <div className="card-actions">
        <button type="button" className="button small" onClick={onEdit}>
          {t("dash.editWidget")}
        </button>
        <button type="button" className="button small danger" onClick={onRemove}>
          {t("common.remove")}
        </button>
      </div>
      <p className="muted small resize-hint">{t("dash.resizeHint")}</p>
    </section>
  );
}
