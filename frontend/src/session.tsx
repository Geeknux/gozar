/** The signed-in user and their preference options (time zone, home dashboard). */
import { createContext, useContext } from "react";

import type { User } from "./App";
import type { Lang } from "./i18n";

export type Session = {
  user: User;
  options: Record<string, unknown>;
  timezone: string;
  /** languages names and descriptions can be given in; the default one is required */
  content: { languages: { value: Lang; label: string; dir: "ltr" | "rtl" }[]; defaultLanguage: Lang };
  /** PATCH /preferences {options: {key: value}} and keep the result */
  setOption: (key: string, value: unknown) => Promise<void>;
};

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside a signed-in page");
  return session;
}

export const homeDashboard = (s: Session): number | null =>
  typeof s.options.home_dashboard === "number" ? s.options.home_dashboard : null;
