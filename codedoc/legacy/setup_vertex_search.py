"""
Setup script for Vertex AI Search datastore.

This script:
1. Creates a Vertex AI Search datastore
2. Uploads all generated documentation (summaries + topics) as documents
3. Prints the datastore ID for use with the query agent

Usage:
    python setup_vertex_search.py [--project PROJECT_ID] [--datastore-id DATASTORE_ID] [--docs-dir DOCS_DIR]

Prerequisites:
    - gcloud auth login (or application-default credentials)
    - pip install google-cloud-discoveryengine google-cloud-storage
"""

import argparse
import os
import hashlib

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine


LOCATION = "global"
COLLECTION = "default_collection"


def _client_options(project_id: str):
    """Build client options with explicit quota project."""
    return ClientOptions(
        quota_project_id=project_id,
    )


def create_datastore(project_id: str, datastore_id: str, display_name: str) -> str:
    """Create a Vertex AI Search datastore."""
    client = discoveryengine.DataStoreServiceClient(client_options=_client_options(project_id))

    parent = client.collection_path(
        project=project_id,
        location=LOCATION,
        collection=COLLECTION,
    )

    data_store = discoveryengine.DataStore(
        display_name=display_name,
        industry_vertical=discoveryengine.IndustryVertical.GENERIC,
        solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
        content_config=discoveryengine.DataStore.ContentConfig.CONTENT_REQUIRED,
    )

    request = discoveryengine.CreateDataStoreRequest(
        parent=parent,
        data_store_id=datastore_id,
        data_store=data_store,
    )

    print(f"Creating datastore '{datastore_id}'...")
    operation = client.create_data_store(request=request)
    response = operation.result()
    print(f"Datastore created: {response.name}")

    return f"projects/{project_id}/locations/{LOCATION}/collections/{COLLECTION}/dataStores/{datastore_id}"


def upload_documents(project_id: str, datastore_id: str, docs_dir: str):
    """Upload markdown documentation files as unstructured documents."""
    client = discoveryengine.DocumentServiceClient(client_options=_client_options(project_id))

    parent = client.branch_path(
        project=project_id,
        location=LOCATION,
        data_store=datastore_id,
        branch="default_branch",
    )

    # Collect all markdown files from the docs directory
    md_files = []
    for root, _, files in os.walk(docs_dir):
        for f in sorted(files):
            if f.endswith(".md"):
                md_files.append(os.path.join(root, f))

    print(f"Found {len(md_files)} markdown files to upload")

    for filepath in md_files:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # Create a stable document ID from the file path
        rel_path = os.path.relpath(filepath, docs_dir)
        doc_id = hashlib.md5(rel_path.encode()).hexdigest()[:63]

        document = discoveryengine.Document(
            id=doc_id,
            content=discoveryengine.Document.Content(
                raw_bytes=content.encode("utf-8"),
                mime_type="text/plain",
            ),
            struct_data={
                "title": os.path.splitext(os.path.basename(filepath))[0],
                "source_path": rel_path,
                "doc_type": "summary" if "summaries" in rel_path else "topic" if "topics" in rel_path else "index",
            },
        )

        request = discoveryengine.CreateDocumentRequest(
            parent=parent,
            document=document,
            document_id=doc_id,
        )

        try:
            client.create_document(request=request)
            print(f"  Uploaded: {rel_path}")
        except Exception as e:
            if "already exists" in str(e).lower():
                # Update existing document
                document.name = f"{parent}/documents/{doc_id}"
                update_request = discoveryengine.UpdateDocumentRequest(
                    document=document,
                    allow_missing=True,
                )
                client.update_document(request=update_request)
                print(f"  Updated: {rel_path}")
            else:
                print(f"  Error uploading {rel_path}: {e}")

    # Also upload the reasoning_index.json as a text document
    index_path = os.path.join(docs_dir, "reasoning_index.json")
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()

        doc_id = "reasoning_index_json"
        document = discoveryengine.Document(
            id=doc_id,
            content=discoveryengine.Document.Content(
                raw_bytes=content.encode("utf-8"),
                mime_type="application/json",
            ),
            struct_data={
                "title": "Reasoning Index",
                "source_path": "reasoning_index.json",
                "doc_type": "index",
            },
        )

        try:
            request = discoveryengine.CreateDocumentRequest(
                parent=parent,
                document=document,
                document_id=doc_id,
            )
            client.create_document(request=request)
            print(f"  Uploaded: reasoning_index.json")
        except Exception:
            document.name = f"{parent}/documents/{doc_id}"
            update_request = discoveryengine.UpdateDocumentRequest(
                document=document,
                allow_missing=True,
            )
            client.update_document(request=update_request)
            print(f"  Updated: reasoning_index.json")

    print(f"\nDone! {len(md_files) + 1} documents uploaded.")


def main():
    parser = argparse.ArgumentParser(description="Setup Vertex AI Search for code documentation")
    parser.add_argument("--project", default="development-459201", help="GCP project ID")
    parser.add_argument("--datastore-id", default="codedoc-index", help="Datastore ID to create")
    parser.add_argument("--docs-dir", default="output_docs", help="Path to generated documentation")
    parser.add_argument("--skip-create", action="store_true", help="Skip datastore creation (already exists)")
    args = parser.parse_args()

    if not os.path.isdir(args.docs_dir):
        print(f"Error: docs directory not found: {args.docs_dir}")
        return

    # Step 1: Create datastore (unless skipping)
    full_datastore_path = f"projects/{args.project}/locations/{LOCATION}/collections/{COLLECTION}/dataStores/{args.datastore_id}"

    if not args.skip_create:
        try:
            full_datastore_path = create_datastore(args.project, args.datastore_id, "Code Documentation Index")
        except Exception as e:
            if "already exists" in str(e).lower():
                print(f"Datastore '{args.datastore_id}' already exists, reusing it.")
            else:
                raise

    # Step 2: Upload documents
    upload_documents(args.project, args.datastore_id, args.docs_dir)

    # Print config for query agent
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nDatastore path for vais_query_agent/.env:")
    print(f"  VERTEX_SEARCH_DATASTORE={full_datastore_path}")
    print(f"\nUpdate vais_query_agent/.env with this value, then run:")
    print(f"  adk run vais_query_agent")


if __name__ == "__main__":
    main()
