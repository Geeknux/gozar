/**
 * Size-dependent chart layout, kept free of ECharts and the DOM so it can be unit
 * tested (chartLayout.test.ts). Chart.tsx measures the container and the label
 * text and asks these functions how to lay the chart out; the answer is part of
 * the chart's option key, so crossing a threshold redraws the chart.
 */

/** Room ECharts needs beside the plot for the value axis and the grid margins. */
const VALUE_AXIS = 64;
/** Space kept between two category labels on a horizontal axis. */
const LABEL_GAP = 8;
/** Below this canvas size a pie has no room for percent labels on its slices. */
export const PIE_LABEL_MIN_SIZE = 220;
/** A slice smaller than this share has no percent label (the legend shows it). */
export const PIE_LABEL_MIN_PERCENT = 6;

export type BarLayout = {
  /** categories on the vertical axis, so every label gets a full line */
  horizontal: boolean;
  /** horizontal only: the width category labels wrap at, in px; 0 when they all fit */
  labelWidth: number;
};

/**
 * A bar chart over a category (not time) field turns horizontal when its labels
 * don't fit side by side under the bars: ECharts would otherwise hide or overlap
 * them. Horizontal labels longer than a third of the width wrap there, never
 * truncate; shorter ones keep their natural width (no wrap width at all, so a small
 * difference between this measurement and ECharts' can't break a word).
 */
export function barLayout(opts: { width: number; labelWidths: number[]; timeAxis: boolean }): BarLayout {
  const { width, labelWidths, timeAxis } = opts;
  const longest = Math.max(0, ...labelWidths);
  const cap = Math.round(Math.max(72, width / 3));
  const labelWidth = longest > cap ? cap : 0;
  if (timeAxis || !labelWidths.length || width <= 0) return { horizontal: false, labelWidth };
  const slot = (width - VALUE_AXIS) / labelWidths.length;
  return { horizontal: longest + LABEL_GAP > slot, labelWidth };
}

/** Whether slices carry their percent: only when the pie is big enough to hold it. */
export const pieSliceLabels = (width: number, height: number) => Math.min(width, height) >= PIE_LABEL_MIN_SIZE;

/** Each value's share of the total (0..1); missing and negative values count as 0. */
export function shares(values: (number | null)[]): number[] {
  const clean = values.map((v) => (v !== null && Number.isFinite(v) && v > 0 ? v : 0));
  const total = clean.reduce((a, b) => a + b, 0);
  return clean.map((v) => (total ? v / total : 0));
}
