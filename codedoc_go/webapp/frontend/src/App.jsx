import { lazy, Suspense } from "react";
import { Routes, Route } from "react-router-dom";
import RepoProvider from "./components/RepoProvider";
import Sidebar from "./components/Sidebar";
import DocsPage from "./pages/DocsPage";
import TopicPage from "./pages/TopicPage";
import SummaryPage from "./pages/SummaryPage";
import ChatPage from "./pages/ChatPage";

// The graph explorer carries Cytoscape.js; it loads when its page opens.
const GraphPage = lazy(() => import("./pages/GraphPage"));

export default function App() {
  return (
    <RepoProvider>
      <div className="app-layout">
        <Sidebar />
        <Routes>
          <Route path="/" element={<DocsPage />} />
          <Route path="/topic/:name" element={<TopicPage />} />
          <Route path="/summary/:kind/:name" element={<SummaryPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route
            path="/graph"
            element={
              <Suspense fallback={<main className="main-content"><div className="empty-state"><p>Loading...</p></div></main>}>
                <GraphPage />
              </Suspense>
            }
          />
        </Routes>
      </div>
    </RepoProvider>
  );
}
