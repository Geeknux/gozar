// The Persian dictionary against the English one: npm test. Its type,
// Record<MessageKey, string>, already makes tsc reject a missing or extra key;
// this checks what the type can't: no empty strings and the same {placeholders}.
import assert from "node:assert/strict";
import { test } from "node:test";

import { en } from "../src/i18n/en.ts";
import { fa } from "../src/i18n/fa.ts";

const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

test("fa has exactly the keys of en", () => {
  assert.deepEqual(Object.keys(fa).sort(), Object.keys(en).sort());
});

test("every fa string is filled in", () => {
  assert.deepEqual(Object.keys(fa).filter((k) => !fa[k].trim()), []);
});

test("fa strings use the same placeholders as en", () => {
  const differ = Object.keys(en).filter((k) => placeholders(en[k]).join() !== placeholders(fa[k] ?? "").join());
  assert.deepEqual(differ, []);
});
