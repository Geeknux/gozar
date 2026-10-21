#!/usr/bin/env node
// Bidi lint (see src/bidi.tsx): JSX that renders user or data text — names, titles,
// descriptions, labels, questions, data cells — must isolate it: inside <bdi>,
// <UserText>, or an element with a dir attribute (on the same line or the opening
// tag just above). A deliberate exception takes a `bidi-ok` comment on the line.
//
//   node scripts/check-bidi.mjs   (exit code 1 and a list of places when it fails)
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../src/", import.meta.url).pathname;
// a JSX child expression whose value is text from users or the database
const DATA_PROPS =
  "name|title|description|label|question|interpretation|clarification|schedule_name|object_repr|username|full_name|filename|problem|suggestion|verbose_name|verbose_name_plural|app_verbose_name|group|display";
const CHILD = new RegExp(
  String.raw`(^|>|\)\s*&&\s*|\?\s*|:\s*)\s*\{([^{}]*?(?:\.(?:${DATA_PROPS})\b|personName\(|fmt\.cell\(|dimensionText\(|cellText\()[^{}]*)\}`,
  "g",
);
const SAFE = /<bdi\b|<UserText\b|\bdir=|\bisolate\(|bidi-ok/;
const OPEN_ABOVE = /<(bdi|Bdi|UserText)\b|\bdir=/;

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else if (name.endsWith(".tsx")) yield path;
  }
}

const problems = [];
for (const path of files(ROOT)) {
  const lines = readFileSync(path, "utf8").split("\n");
  lines.forEach((line, i) => {
    if (SAFE.test(line)) return;
    const trimmed = line.trim();
    if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("import ")) return;
    for (const match of line.matchAll(CHILD)) {
      const expr = match[2].trim();
      // attribute values (title={x.name}), t() parameters and plain code are not JSX children
      const before = line.slice(0, match.index + match[1].length).trimEnd();
      if (/[=(,]$/.test(before) || /^\s*[a-zA-Z_$][\w$]*\s*:/.test(expr) || /^(t\(|\.\.\.)/.test(expr)) continue;
      if (!/^(\s*\{|.*>\s*\{|.*\)\s*&&\s*\{|.*\?\s*\{|.*:\s*\{)/.test(line.slice(0, match.index + match[0].indexOf("{") + 1))) continue;
      // the text may sit in an element opened on one of the lines above
      const above = lines.slice(Math.max(0, i - 3), i).join("\n");
      const lastOpen = above.search(OPEN_ABOVE);
      if (lastOpen >= 0 && !/<\/(bdi|Bdi|UserText)>/.test(above.slice(lastOpen))) continue;
      problems.push(`${relative(process.cwd(), path)}:${i + 1}: {${expr}}`);
    }
  });
}

if (problems.length) {
  console.error("User or data text rendered without bidi isolation (wrap it in <bdi>, <UserText> or add dir=\"auto\"; see src/bidi.tsx):");
  for (const p of problems) console.error("  " + p);
  process.exit(1);
}
console.log("bidi lint: ok");
