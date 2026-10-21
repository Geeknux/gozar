import { BarChart as Bars } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import { isolate } from "../bidi";
import { useI18n } from "../i18n";

echarts.use([Bars, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

type Series = { name: string; data: (number | null)[] };
type Props = {
  label: string;
  categories: string[];
  series: Series[];
  /** horizontal bars (long category names such as table names) */
  horizontal?: boolean;
  stacked?: boolean;
  format: (v: number) => string;
  height?: number;
};

/** A bar chart in the user's digits and direction (the System page's charts). */
export default function BarChart({ label, categories, series, horizontal, stacked, format, height = 280 }: Props) {
  const { dir, lang } = useI18n();
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const rtl = dir === "rtl";

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

  const key = JSON.stringify([categories, series, horizontal, stacked, dir, lang]);
  useEffect(() => {
    const instance = chart.current;
    if (!instance) return;
    const font = getComputedStyle(document.documentElement).fontFamily;
    const valueAxis = {
      type: "value",
      axisLabel: { formatter: (v: number) => format(v), hideOverlap: true },
    };
    // table names and labels keep their own direction on the canvas (bidi.tsx)
    const categoryAxis = { type: "category", data: categories.map((c) => isolate(c)), axisLabel: { hideOverlap: !horizontal } };
    instance.setOption(
      {
        textStyle: { fontFamily: font },
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v: unknown) => format(Number(v)) },
        legend: series.length > 1 ? { top: 0, [rtl ? "right" : "left"]: 0 } : undefined,
        grid: { top: series.length > 1 ? 32 : 8, bottom: 8, left: 8, right: 16, containLabel: true },
        // Horizontal: names read from the start edge, the longest bar first.
        xAxis: horizontal ? { ...valueAxis, inverse: rtl } : { ...categoryAxis, inverse: rtl },
        yAxis: horizontal
          ? { ...categoryAxis, inverse: true, position: rtl ? "right" : "left" }
          : { ...valueAxis, position: rtl ? "right" : "left" },
        series: series.map((s) => ({
          type: "bar",
          name: isolate(s.name),
          data: s.data,
          stack: stacked ? "total" : undefined,
          emphasis: { focus: "series" },
        })),
      },
      { notMerge: true },
    );
    // format is rebuilt every render; key captures what the option depends on
  }, [key]);

  return <div ref={el} className="chart sys-chart" style={{ height }} role="img" aria-label={label} />;
}
