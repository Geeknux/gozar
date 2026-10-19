import { type FormEvent, useEffect, useRef, useState } from "react";

import { api } from "../api";
import { useI18n } from "../i18n";
import { compact, type Period, type Spec } from "./model";

/** POST /nl/ask (bi_admin.nl.pipeline.Answer). */
export type Answer = {
  status: "ok" | "clarification" | "invalid";
  question: string;
  spec: Spec | null;
  interpretation: string;
  clarification: string;
  suggestions: { label: string; question: string }[];
  warnings: string[];
  period: Period | null;
  provider: { name: string; model: string };
  attempts: number;
  retries: number;
  cached: boolean;
};

type Status = { enabled: boolean; provider: { name: string; model: string }; llm: boolean; max_length: number; examples: string[] };

type Turn = { question: string; answer?: Answer; error?: string };

type Props = {
  /** The report a follow-up refines ("now only Tehran"); usually the last answer's spec. */
  current: Spec | null;
  onSpec: (spec: Spec, answer: Answer) => void;
  autoFocus?: boolean;
};

/** "Ask a question": the user's words in, a report spec out, with the interpretation
 * shown for every answer. Once a question has been answered, the next one refines
 * the current report until "New question" starts over. */
export default function AskBox({ current, onSpec, autoFocus }: Props) {
  const { t } = useI18n();
  const [status, setStatus] = useState<Status | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api<Status>("/nl/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  const refining = turns.some((turn) => turn.answer?.status === "ok") && current !== null;

  async function ask(question: string) {
    question = question.trim();
    if (!question || busy) return;
    setBusy(true);
    setTurns((all) => [...all, { question }]);
    setText("");
    const update = (turn: Turn) => setTurns((all) => [...all.slice(0, -1), turn]);
    try {
      const answer = await api<Answer>("/nl/ask", {
        method: "POST",
        body: { question, previous: refining && current ? compact(current) : null },
      });
      update({ question, answer });
      if (answer.status === "ok" && answer.spec) onSpec(compact(answer.spec), answer);
    } catch (err) {
      update({ question, error: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      input.current?.focus();
    }
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void ask(text);
  };

  if (status && !status.enabled) return null;
  const last = turns[turns.length - 1];
  const provider = last?.answer?.provider ?? status?.provider;
  const maxLength = status?.max_length || undefined;

  return (
    <section className="ask-box card" aria-label={t("ask.title")}>
      <form onSubmit={submit} className="ask-form">
        <input
          ref={input}
          type="text"
          dir="auto"
          value={text}
          maxLength={maxLength}
          autoFocus={autoFocus}
          placeholder={refining ? t("ask.followUpPlaceholder") : t("ask.placeholder")}
          aria-label={t("ask.title")}
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit" className="primary" disabled={busy || !text.trim()}>
          {busy ? t("ask.thinking") : t("ask.submit")}
        </button>
        {turns.length > 0 && (
          <button type="button" onClick={() => setTurns([])} disabled={busy}>
            {t("ask.newQuestion")}
          </button>
        )}
      </form>
      {turns.length === 0 && status && status.examples.length > 0 && (
        <div className="ask-examples">
          <span className="muted">{t("ask.tryExamples")}</span>
          {status.examples.map((example) => (
            <button key={example} type="button" className="chip-button" dir="auto" onClick={() => ask(example)}>
              {example}
            </button>
          ))}
        </div>
      )}
      {turns.length > 0 && (
        <ol className="ask-thread">
          {turns.map((turn, i) => (
            <li key={i}>
              <p className="ask-question" dir="auto">
                {turn.question}
              </p>
              {!turn.answer && !turn.error && <p className="muted">{t("ask.thinking")}</p>}
              {turn.error && <p className="error">{turn.error}</p>}
              {turn.answer && <AnswerView answer={turn.answer} onAsk={ask} latest={i === turns.length - 1} />}
            </li>
          ))}
        </ol>
      )}
      <p className="ask-footer muted">
        {provider && (provider.name === "heuristic" ? t("ask.offline") : t("ask.model", { model: provider.model || provider.name }))}
        {refining && <> · {t("ask.refining")}</>}
      </p>
    </section>
  );
}

function AnswerView({ answer, onAsk, latest }: { answer: Answer; onAsk: (q: string) => void; latest: boolean }) {
  const { t } = useI18n();
  return (
    <div className={`ask-answer status-${answer.status}`}>
      {answer.status === "ok" ? (
        <p dir="auto">
          <span className="muted">{t("ask.understood")}</span> {answer.interpretation}
        </p>
      ) : (
        <p dir="auto" className={answer.status === "invalid" ? "error" : "ask-clarify"}>
          {answer.clarification}
        </p>
      )}
      {latest && answer.suggestions.length > 0 && (
        <div className="ask-examples">
          {answer.suggestions.map((s) => (
            <button key={s.question} type="button" className="chip-button" dir="auto" onClick={() => onAsk(s.question)}>
              {s.label}
            </button>
          ))}
        </div>
      )}
      {answer.warnings.map((w) => (
        <p key={w} className="muted small" dir="auto">
          {w}
        </p>
      ))}
    </div>
  );
}
