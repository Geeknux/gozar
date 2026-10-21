/**
 * Bidirectional text. Names, titles, descriptions and data values come from users
 * and databases in either language, so the page's direction says nothing about
 * theirs: English inside a Persian page (or Persian inside an English one) must be
 * isolated, or its punctuation and neighbours get reordered.
 *
 * - inline text: `<bdi>`; a block: {@link UserText} (dir="auto", each paragraph
 *   takes the direction of its first strong letter);
 * - plain strings that end up outside the DOM's bidi handling — ECharts canvas
 *   labels and tooltips, `title=` tooltips, `confirm()` prompts, translated messages
 *   with a user value in them — go through {@link isolate}; `t()` does it for every
 *   string parameter.
 *
 * `npm run lint:bidi` (part of `npm run build`) flags JSX that renders such a value
 * without one of these.
 */
import type { ElementType, ReactNode } from "react";

const FSI = "\u2068"; // first-strong isolate
const PDI = "\u2069"; // pop directional isolate
const LETTER = /\p{L}/u;

/** `text` wrapped in Unicode isolates, so it keeps its own direction wherever it is
 * placed. Text without letters (numbers, dates, symbols) is returned unchanged. */
export function isolate(text: string): string;
export function isolate(text: string | null | undefined): string | null | undefined;
export function isolate(text: string | null | undefined) {
  if (!text || !LETTER.test(text) || (text.startsWith(FSI) && text.endsWith(PDI))) return text;
  return FSI + text + PDI;
}

type UserTextProps = {
  children: ReactNode;
  /** element to render (default `p`) */
  as?: ElementType;
  className?: string;
  title?: string;
  id?: string;
};

/** A block of user or data text (a description, a message, an interpretation):
 * `dir="auto"` plus the `user-text` class (`unicode-bidi: plaintext; text-align:
 * start`), so every line reads in its own direction. */
export function UserText({ children, as: Tag = "p", className, ...rest }: UserTextProps) {
  return (
    <Tag dir="auto" className={className ? `user-text ${className}` : "user-text"} {...rest}>
      {children}
    </Tag>
  );
}
