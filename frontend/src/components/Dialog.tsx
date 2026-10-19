import { type ReactNode, useEffect } from "react";

type Props = { title: string; onClose: () => void; children: ReactNode; wide?: boolean };

/** A modal card: closes on Escape and on a click outside it. */
export default function Dialog({ title, onClose, children, wide }: Props) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`card dialog${wide ? " wide" : ""}`}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}
