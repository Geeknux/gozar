import { useParams, useSearchParams } from "react-router-dom";

import DashboardView from "./DashboardView";

export default function DashboardPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  // keyed by id: another dashboard starts from a clean state
  return <DashboardView key={id} id={Number(id)} startEditing={params.get("edit") === "1"} />;
}
