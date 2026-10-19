import { useEffect, useState } from "react";

import { useI18n } from "../i18n";
import { type GroupRef, type Person, personName, shareTargets } from "../sharing";

type Props = {
  users: Person[];
  groups?: GroupRef[];
  onChange: (users: Person[], groups: GroupRef[]) => void;
  /** offer groups too (sharing) or only users (e-mail recipients) */
  withGroups?: boolean;
};

/** Chips of chosen users (and groups) plus a search box over GET /sharing/targets. */
export default function PeoplePicker({ users, groups = [], onChange, withGroups = true }: Props) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  const [found, setFound] = useState<{ users: Person[]; groups: GroupRef[] }>({ users: [], groups: [] });
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => {
      shareTargets(q)
        .then(setFound)
        .catch(() => setFound({ users: [], groups: [] }));
    }, 200);
    return () => clearTimeout(timer);
  }, [q, open]);

  const userIds = new Set(users.map((u) => u.id));
  const groupIds = new Set(groups.map((g) => g.id));
  const candidates = found.users.filter((u) => !userIds.has(u.id));
  const groupCandidates = withGroups ? found.groups.filter((g) => !groupIds.has(g.id)) : [];

  return (
    <div className="people-picker">
      <ul className="chips">
        {users.map((u) => (
          <li key={`u${u.id}`}>
            <bdi>{personName(u)}</bdi>
            <button type="button" aria-label={t("common.remove")} onClick={() => onChange(users.filter((x) => x.id !== u.id), groups)}>
              ×
            </button>
          </li>
        ))}
        {groups.map((g) => (
          <li key={`g${g.id}`} className="group-chip">
            <bdi>{g.name}</bdi>
            <button type="button" aria-label={t("common.remove")} onClick={() => onChange(users, groups.filter((x) => x.id !== g.id))}>
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="relation-select">
        <input
          type="search"
          value={q}
          placeholder={withGroups ? t("share.searchPeople") : t("share.searchUsers")}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onChange={(e) => setQ(e.target.value)}
        />
        {open && (candidates.length > 0 || groupCandidates.length > 0) && (
          <div className="relation-popup">
            <ul>
              {candidates.map((u) => (
                <li key={`u${u.id}`}>
                  <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => onChange([...users, u], groups)}>
                    <bdi>{personName(u)}</bdi> {u.full_name && <span className="muted">({u.username})</span>}
                  </button>
                </li>
              ))}
              {groupCandidates.map((g) => (
                <li key={`g${g.id}`}>
                  <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => onChange(users, [...groups, g])}>
                    <span className="muted">{t("share.group")}:</span> <bdi>{g.name}</bdi>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
