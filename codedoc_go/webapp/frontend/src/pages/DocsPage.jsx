import MarkdownView from "../components/MarkdownView";
import { useJson, useRepo, withRepo } from "../repo";

export default function DocsPage() {
  const { ready, selected } = useRepo();
  const { data: doc, error } = useJson(ready ? withRepo("/api/docs/index", selected) : null);

  if (error) {
    return (
      <main className="main-content">
        <div className="empty-state">
          <h3>Index not found</h3>
          <p>{error}</p>
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
