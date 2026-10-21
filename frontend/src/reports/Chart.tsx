import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef, useState } from "react";

import { isolate } from "../bidi";
import { useI18n } from "../i18n";
import { barLayout, PIE_LABEL_MIN_PERCENT, pieSliceLabels, shares } from "./chartLayout";
import { type Cell, type Column, comparisonColumn, dimensionColumns, dimensionText, type Result, type Scalar } from "./model";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

type Kind = "bar" | "line" | "area" | "pie";
type Props = {
  result: Result;
  kind: Kind;
  measure: Column;
  onDrill: (cell: Cell, measure: string, comparison?: boolean) => void;
};

const uniq = (values: (Scalar | null)[]) => {
  const seen = new Map<string, Scalar | null>();
  for (const v of values) if (!seen.has(JSON.stringify(v))) seen.set(JSON.stringify(v), v);
  return [...seen.values()];
};

let measurer: CanvasRenderingContext2D | null = null;
/** Width of each text in ECharts' default 12px label font, with a few pixels to spare:
 * ECharts' own measurement differs slightly, and a label wrap width even a pixel
 * short of the text breaks a word in two. */
const textWidths = (texts: string[], font: string) => {
  measurer ??= document.createElement("canvas").getContext("2d");
  if (!measurer) return texts.map((x) => x.length * 8);
  measurer.font = `12px ${font}`;
  return texts.map((x) => Math.ceil(measurer!.measureText(x).width) + 6);
};

/**
 * One measure over the first group-by field; a second group-by field splits it
 * into series. Labels, numbers, digits and direction follow the user's language;
 * time buckets are labelled in the report's calendar. ECharts draws on a canvas in
 * the page's direction, so every label (categories, series, tooltips) is isolated:
 * an English name keeps its order in a Persian chart and vice versa.
 *
 * Narrow containers (dashboard widgets) get a layout that keeps every label whole:
 * a pie lists its slices in an HTML legend that wraps (beside the pie when there is
 * room, under it otherwise) and shows only percents on the slices; a bar chart whose
 * category labels don't fit side by side turns horizontal (chartLayout.ts).
 */
export default function Chart({ result, kind, measure, onDrill }: Props) {
  const i18n = useI18n();
  const { t, fmt, dir } = i18n;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const drill = useRef(onDrill);
  drill.current = onDrill;
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [palette, setPalette] = useState<string[]>([]);

  const dims = dimensionColumns(result);
  const [first, second] = dims;
  const categories = uniq(result.rows.map((r) => r[first.key] ?? null));
  const splits = second ? uniq(result.rows.map((r) => r[second.key] ?? null)) : [null];
  const lookup = new Map(
    result.rows.map((r) => [JSON.stringify([r[first.key] ?? null, second ? (r[second.key] ?? null) : null]), r]),
  );
  const valueAt = (cat: Scalar | null, split: Scalar | null, key = measure.key) => {
    const row = lookup.get(JSON.stringify([cat, split]));
    const v = row?.[key];
    return v === null || v === undefined ? null : Number(v);
  };
  // With a comparison period (and no second group-by), it becomes a second series.
  const compare = !second && kind !== "pie" ? comparisonColumn(result, measure.key, "compare") : undefined;
  const rtl = dir === "rtl";
  const font = getComputedStyle(document.documentElement).fontFamily;
  const catLabels = categories.map((c) => isolate(dimensionText(first, c, fmt, t)));
  const valueText = (v: unknown) => fmt.measure(v, measure.format);
  const percentText = (share: number) => fmt.measure(share, "percent");
  const pie = kind === "pie";
  const pieValues = pie ? categories.map((c) => valueAt(c, null)) : [];
  const pieShares = shares(pieValues);
  const sliceLabels = pie && pieSliceLabels(size.width, size.height);
  const bars =
    kind === "bar"
      ? barLayout({ width: size.width, labelWidths: textWidths(catLabels, font), timeAxis: first.type === "time" })
      : { horizontal: false, labelWidth: 0 };
  const horizontal = bars.horizontal;

  let option: echarts.EChartsCoreOption;
  if (pie) {
    option = {
      textStyle: { fontFamily: font },
      tooltip: {
        trigger: "item",
        confine: true,
        formatter: (p: { name: string; value: number; percent: number }) =>
          `${p.name}<br/>${valueText(p.value)} (${percentText(p.percent / 100)})`,
      },
      series: [
        {
          type: "pie",
          radius: ["38%", "70%"],
          center: ["50%", "50%"],
          data: categories.map((_, i) => ({ name: catLabels[i], value: pieValues[i] })),
          // names live in the legend beside the chart; slices carry only their share
          label: {
            show: sliceLabels,
            position: "inside",
            color: "#fff",
            fontWeight: 600,
            textBorderColor: "rgba(0, 0, 0, 0.35)",
            textBorderWidth: 2,
            formatter: (p: { percent: number }) => (p.percent >= PIE_LABEL_MIN_PERCENT ? percentText(p.percent / 100) : ""),
          },
          labelLine: { show: false },
        },
      ],
    };
  } else {
    const categoryAxis = {
      type: "category",
      data: catLabels,
      ...(horizontal
        ? {
            // first category on top; labels wrap rather than truncate
            inverse: true,
            position: rtl ? "right" : "left",
            axisLabel: bars.labelWidth ? { width: bars.labelWidth, overflow: "break", interval: 0 } : { interval: 0 },
          }
        : { inverse: rtl, axisLabel: { hideOverlap: true } }),
    };
    const valueAxis = {
      type: "value",
      ...(horizontal ? { inverse: rtl } : { position: rtl ? "right" : "left" }),
      axisLabel: {
        hideOverlap: true,
        formatter: (v: number) => (measure.format === "percent" ? fmt.measure(v, "percent") : fmt.compact(v)),
      },
    };
    option = {
      textStyle: { fontFamily: font },
      tooltip: { trigger: "axis", confine: true, valueFormatter: valueText },
      legend: second || compare ? { type: "scroll", top: 0, [rtl ? "right" : "left"]: 0 } : undefined,
      grid: { top: second || compare ? 40 : 16, bottom: 8, left: 16, right: 16, containLabel: true },
      xAxis: horizontal ? valueAxis : categoryAxis,
      yAxis: horizontal ? categoryAxis : valueAxis,
      series: [
        ...splits.map((s) => ({
          type: kind === "bar" ? "bar" : "line",
          name: isolate(second ? dimensionText(second, s, fmt, t) : measure.label),
          data: categories.map((c) => valueAt(c, s)),
          areaStyle: kind === "area" ? { opacity: 0.25 } : undefined,
          connectNulls: kind !== "bar",
          emphasis: { focus: "series" },
        })),
        ...(compare
          ? [
              {
                type: kind === "bar" ? "bar" : "line",
                name: isolate(compare.label),
                data: categories.map((c) => valueAt(c, null, compare.key)),
                connectNulls: kind !== "bar",
                lineStyle: { type: "dashed" },
                itemStyle: { opacity: kind === "bar" ? 0.45 : 0.8 },
                emphasis: { focus: "series" },
              },
            ]
          : []),
      ],
    };
  }

  useEffect(() => {
    if (!el.current) return;
    const instance = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const observer = new ResizeObserver(([entry]) => {
      instance.resize();
      const { width, height } = entry.contentRect;
      // whole tens of pixels: enough for the layout thresholds, without a render per pixel
      setSize((s) => {
        const next = { width: Math.round(width / 10) * 10, height: Math.round(height / 10) * 10 };
        return next.width === s.width && next.height === s.height ? s : next;
      });
    });
    observer.observe(el.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  const optionKey = JSON.stringify([
    kind,
    measure.key,
    result.rows,
    result.columns.length,
    dir,
    i18n.lang,
    i18n.calendar,
    sliceLabels,
    horizontal,
    bars.labelWidth,
  ]);
  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    instance.setOption(option, { notMerge: true });
    const colors = (instance.getOption() as { color?: string[] }).color ?? [];
    setPalette((p) => (p.join() === colors.join() ? p : colors));
    instance.off("click");
    instance.on("click", (params) => {
      const p = params as { dataIndex: number; seriesIndex?: number };
      const cell: Cell = { [first.key]: categories[p.dataIndex] ?? null };
      if (second && kind !== "pie") cell[second.key] = splits[p.seriesIndex ?? 0] ?? null;
      drill.current(cell, measure.key, Boolean(compare) && p.seriesIndex === splits.length);
    });
    // option is rebuilt every render; optionKey captures what it depends on
  }, [optionKey]);

  const highlight = (type: "highlight" | "downplay", dataIndex: number) =>
    chart.current?.dispatchAction({ type, seriesIndex: 0, dataIndex });

  // The canvas element stays first in the same place whatever the kind, so switching
  // to and from a pie keeps the ECharts instance bound to it.
  return (
    <div className={pie ? "chart chart-pie" : "chart"}>
      <div className="chart-body">
        <div ref={el} className="chart-canvas" role="img" aria-label={isolate(measure.label)} />
        {pie && (
          <ul className="chart-legend" aria-label={isolate(first.label)}>
            {categories.map((c, i) => (
              <li key={JSON.stringify(c)}>
                <button
                  type="button"
                  title={`${catLabels[i]}: ${valueText(pieValues[i])} (${percentText(pieShares[i])})`}
                  onMouseEnter={() => highlight("highlight", i)}
                  onMouseLeave={() => highlight("downplay", i)}
                  onFocus={() => highlight("highlight", i)}
                  onBlur={() => highlight("downplay", i)}
                  onClick={() => drill.current({ [first.key]: c }, measure.key)}
                >
                  <span className="chart-swatch" style={{ background: palette.length ? palette[i % palette.length] : undefined }} />
                  <span className="chart-legend-name">{catLabels[i]}</span>
                  <span className="chart-legend-value">{percentText(pieShares[i])}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
