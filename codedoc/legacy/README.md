# legacy/ — archived components

These are **retired** parts of CodeDoc, kept for reference only. Nothing in the
active system imports them. The current system is **graph-RAG**:

- **Generation:** `graph_generator/` (`python -m graph_generator analyze <dir>`) — builds
  the docs + the Cloud Spanner code knowledge graph.
- **Retrieval/query:** `graph_query_agent/` (Spanner GQL), exposed via `mcp_server/` and the
  `webapp/` chat. There is no Vertex AI Search in the active path anymore.

## What's here

| Path | What it was | Replaced by |
|------|-------------|-------------|
| `code_doc_agent/` | Original Google ADK 6-phase documentation pipeline (LLM-only entity extraction). | `graph_generator/` (Tree-sitter + Vertex AI SDK, 10-phase, graph). |
| `vais_query_agent/` | Query agent backed by Vertex AI Search (semantic doc lookup). | `graph_query_agent/` (Spanner code graph). |
| `setup_vertex_search.py`, `setup_vertex_search_gcs.py` | Scripts to create/populate the Vertex AI Search datastore. | Not needed — retrieval is graph-only. |

## Running an archived agent (if you really need to)

These are standard ADK agents. From the repository root:

```bash
adk run legacy/code_doc_agent
adk run legacy/vais_query_agent
```

`vais_query_agent` still expects a populated Vertex AI Search datastore
(`VERTEX_SEARCH_DATASTORE`), which the active system no longer provisions.
