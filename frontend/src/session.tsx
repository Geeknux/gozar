/** The signed-in user and their preference options (time zone, home dashboard). */
import { createContext, useContext } from "react";

import type { User } from "./App";

export type Session = {
  user: User;
  options: Record<string, unknown>;
  timezone: string;
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
