// Unit tests for the chart layout rules: npm test (node --test; Node strips the types).
import assert from "node:assert/strict";
import { test } from "node:test";

import { barLayout, pieSliceLabels, shares } from "../src/reports/chartLayout.ts";

test("bars stay vertical while the category labels fit side by side", () => {
  const layout = barLayout({ width: 900, labelWidths: [40, 52, 38, 61], timeAxis: false });
  assert.equal(layout.horizontal, false);
});

test("bars turn horizontal in a narrow widget, with labels wrapping instead of truncating", () => {
  const labels = [40, 52, 38, 61, 70, 45, 58, 66];
  const layout = barLayout({ width: 360, labelWidths: labels, timeAxis: false });
  assert.equal(layout.horizontal, true);
  assert.equal(layout.labelWidth, 0); // the longest label fits in a third of the width: no wrapping
});

test("a long label never gets more than a third of the width", () => {
  const layout = barLayout({ width: 600, labelWidths: [480, 90, 120], timeAxis: false });
  assert.equal(layout.horizontal, true);
  assert.equal(layout.labelWidth, 200);
});

test("time series keep their time axis horizontal however narrow", () => {
  const layout = barLayout({ width: 240, labelWidths: Array(12).fill(60), timeAxis: true });
  assert.equal(layout.horizontal, false);
});

test("before the first measurement (width 0) nothing flips", () => {
  assert.equal(barLayout({ width: 0, labelWidths: [300], timeAxis: false }).horizontal, false);
});

test("slices carry percents only when the pie has room", () => {
  assert.equal(pieSliceLabels(400, 260), true);
  assert.equal(pieSliceLabels(400, 200), false);
  assert.equal(pieSliceLabels(200, 400), false);
});

test("legend shares ignore missing and negative values", () => {
  assert.deepEqual(shares([30, null, 10, -5]), [0.75, 0, 0.25, 0]);
  assert.deepEqual(shares([null, 0]), [0, 0]);
});
