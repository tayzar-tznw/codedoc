import { useParams } from "react-router-dom";
import MarkdownView from "../components/MarkdownView";
import { useJson, useRepo, withRepo } from "../repo";

export default function SummaryPage() {
  const { kind, name } = useParams();
  const { ready, selected } = useRepo();
  const url = ready
    ? withRepo(`/api/docs/summaries/${kind}/${encodeURIComponent(name)}`, selected)
    : null;
  const { data: doc, error } = useJson(url);

  if (error) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <h3>Summary not found</h3>
          <p>{name}</p>
        </div>
      </main>
    );
  }

  if (!doc) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <p>Loading...</p>
        </div>
      </main>
    );
  }

  return (
    <main className="main-content">
      <MarkdownView content={doc.markdown} />
    </main>
  );
}
