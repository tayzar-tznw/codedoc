import os

from google.adk.agents import Agent
from google.adk.tools import VertexAiSearchTool

MODEL = "gemini-3.5-flash"

DATASTORE_ID = os.environ.get(
    "VERTEX_SEARCH_DATASTORE",
    "projects/development-459201/locations/global/collections/default_collection/dataStores/codedoc-index",
)

root_agent = Agent(
    name="vais_query_agent",
    model=MODEL,
    description="Answers questions about a codebase using Vertex AI Search grounded documentation.",
    instruction="""You answer questions about a codebase using ONLY the documentation indexed in Vertex AI Search.
You do NOT have access to the source code. All your knowledge comes from the pre-indexed
documentation which includes file summaries, directory summaries, module/topic documentation,
and a structured reasoning index.

Your answers should:
- Be specific and reference actual file names, classes, and methods from the documentation
- Explain how components work together based on the indexed summaries
- Reference the documentation sources that informed your answer
- Be structured with clear sections

If the search doesn't return relevant results, say so clearly and suggest
what the user might search for instead.""",
    tools=[VertexAiSearchTool(data_store_id=DATASTORE_ID)],
)
