import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import { useI18n } from "../i18n";
import { type Cell, type Column, dimensionColumns, dimensionText, type Result, type Scalar } from "./model";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

type Kind = "bar" | "line" | "area" | "pie";
type Props = {
  result: Result;
  kind: Kind;
  measure: Column;
  onDrill: (cell: Cell, measure: string) => void;
};

const uniq = (values: (Scalar | null)[]) => {
  const seen = new Map<string, Scalar | null>();
  for (const v of values) if (!seen.has(JSON.stringify(v))) seen.set(JSON.stringify(v), v);
  return [...seen.values()];
};

/**
 * One measure over the first group-by field; a second group-by field splits it
 * into series. Labels, numbers, digits and direction follow the user's language;
 * time buckets are labelled in the report's calendar.
 */
export default function Chart({ result, kind, measure, onDrill }: Props) {
  const i18n = useI18n();
  const { t, fmt, dir } = i18n;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const drill = useRef(onDrill);
  drill.current = onDrill;

  const dims = dimensionColumns(result);
  const [first, second] = dims;
  const categories = uniq(result.rows.map((r) => r[first.key] ?? null));
  const splits = second ? uniq(result.rows.map((r) => r[second.key] ?? null)) : [null];
  const lookup = new Map(
    result.rows.map((r) => [JSON.stringify([r[first.key] ?? null, second ? (r[second.key] ?? null) : null]), r]),
  );
  const valueAt = (cat: Scalar | null, split: Scalar | null) => {
    const row = lookup.get(JSON.stringify([cat, split]));
    const v = row?.[measure.key];
    return v === null || v === undefined ? null : Number(v);
  };
  const rtl = dir === "rtl";
  const font = getComputedStyle(document.documentElement).fontFamily;
  const catLabels = categories.map((c) => dimensionText(first, c, fmt, t));
  const valueText = (v: unknown) => fmt.measure(v, measure.format);

  let option: echarts.EChartsCoreOption;
  if (kind === "pie") {
    option = {
      textStyle: { fontFamily: font },
      tooltip: {
        trigger: "item",
        formatter: (p: { name: string; value: number; percent: number }) =>
          `${p.name}<br/>${valueText(p.value)} (${fmt.measure(p.percent / 100, "percent")})`,
      },
      legend: { type: "scroll", bottom: 0, [rtl ? "right" : "left"]: 0 },
      series: [
        {
          type: "pie",
          radius: ["35%", "68%"],
          center: ["50%", "45%"],
          data: categories.map((c, i) => ({ name: catLabels[i], value: valueAt(c, null) })),
          label: { formatter: (p: { name: string; percent: number }) => `${p.name}: ${fmt.measure(p.percent / 100, "percent")}` },
        },
      ],
    };
  } else {
    option = {
      textStyle: { fontFamily: font },
      tooltip: { trigger: "axis", valueFormatter: valueText },
      legend: second ? { type: "scroll", top: 0, [rtl ? "right" : "left"]: 0 } : undefined,
      grid: { top: second ? 40 : 16, bottom: 8, left: 16, right: 16, containLabel: true },
      xAxis: {
        type: "category",
        data: catLabels,
        inverse: rtl,
        axisLabel: { hideOverlap: true },
      },
      yAxis: {
        type: "value",
        position: rtl ? "right" : "left",
        axisLabel: {
          formatter: (v: number) => (measure.format === "percent" ? fmt.measure(v, "percent") : fmt.compact(v)),
        },
      },
      series: splits.map((s) => ({
        type: kind === "bar" ? "bar" : "line",
        name: second ? dimensionText(second, s, fmt, t) : measure.label,
        data: categories.map((c) => valueAt(c, s)),
        areaStyle: kind === "area" ? { opacity: 0.25 } : undefined,
        connectNulls: kind !== "bar",
        emphasis: { focus: "series" },
      })),
    };
  }

  useEffect(() => {
    if (!el.current) return;
    const instance = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(el.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  const optionKey = JSON.stringify([kind, measure.key, result.rows, dir, i18n.lang, i18n.calendar]);
  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    instance.setOption(option, { notMerge: true });
    instance.off("click");
    instance.on("click", (params) => {
      const p = params as { dataIndex: number; seriesIndex?: number };
      const cell: Cell = { [first.key]: categories[p.dataIndex] ?? null };
      if (second && kind !== "pie") cell[second.key] = splits[p.seriesIndex ?? 0] ?? null;
      drill.current(cell, measure.key);
    });
    // option is rebuilt every render; optionKey captures what it depends on
  }, [optionKey]);

  return <div ref={el} className="chart" role="img" aria-label={measure.label} />;
}
