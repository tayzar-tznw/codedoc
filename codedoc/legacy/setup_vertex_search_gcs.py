"""
Upload generated documentation to GCS and import into Vertex AI Search.

This script:
1. Uploads all generated docs (summaries + topics) to a GCS bucket
2. Creates a Vertex AI Search datastore (if needed)
3. Imports documents from GCS into the datastore

Usage:
    python setup_vertex_search_gcs.py --docs-dir output_docs_pipeline
    python setup_vertex_search_gcs.py --skip-upload --skip-create  # re-import only
    python setup_vertex_search_gcs.py --docs-dir output_docs_pipeline --datastore-id my-datastore

Prerequisites:
    pip install google-cloud-discoveryengine google-cloud-storage
"""

import argparse
import os

from dotenv import load_dotenv
load_dotenv()

from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine, storage


LOCATION = "global"
COLLECTION = "default_collection"


def _client_options(project_id: str):
    """Build client options with explicit quota project."""
    return ClientOptions(quota_project_id=project_id)


def upload_to_gcs(project_id: str, bucket_name: str, gcs_prefix: str, docs_dir: str) -> int:
    """Upload documentation files to GCS using transfer_manager for concurrency.

    Args:
        project_id: GCP project ID.
        bucket_name: GCS bucket name.
        gcs_prefix: Prefix path in the bucket (e.g., 'csharp-sample').
        docs_dir: Local directory containing generated docs.

    Returns:
        Number of files uploaded.
    """
    from google.cloud.storage import transfer_manager
    import time

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    # Collect all files
    filenames = []
    blob_names = []
    for root, _, files in os.walk(docs_dir):
        for fname in sorted(files):
            if not (fname.endswith(".md") or fname.endswith(".json")):
                continue
            local_path = os.path.join(root, fname)
            rel_path = os.path.relpath(local_path, docs_dir)
            filenames.append(local_path)
            # Vertex AI Search detects MIME type from the GCS object
            # extension — .md resolves to text/markdown which is rejected.
            # Rename to .txt so it resolves to text/plain.
            if rel_path.endswith(".md"):
                rel_path = rel_path[:-3] + ".txt"
            blob_names.append(f"{gcs_prefix}/{rel_path}")

    total = len(filenames)
    print(f"  Uploading {total} files via transfer_manager...")
    t0 = time.time()

    pairs = []
    for fn, bn in zip(filenames, blob_names):
        blob = bucket.blob(bn)
        if bn.endswith(".txt"):
            blob.content_type = "text/plain"
        elif bn.endswith(".json"):
            blob.content_type = "application/json"
        pairs.append((fn, blob))

    results = transfer_manager.upload_many(
        file_blob_pairs=pairs,
        max_workers=50,
        worker_type=transfer_manager.THREAD,
    )

    errors = sum(1 for r in results if isinstance(r, Exception))
    uploaded = total - errors
    elapsed = time.time() - t0
    print(f"  Uploaded {uploaded}/{total} files to gs://{bucket_name}/{gcs_prefix}/ in {elapsed:.1f}s")
    if errors:
        print(f"  {errors} upload errors")
    return uploaded


def create_datastore(project_id: str, datastore_id: str, display_name: str) -> str:
    """Create a Vertex AI Search datastore.

    Returns:
        Full datastore resource path.
    """
    client = discoveryengine.DataStoreServiceClient(
        client_options=_client_options(project_id)
    )

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

    return (
        f"projects/{project_id}/locations/{LOCATION}"
        f"/collections/{COLLECTION}/dataStores/{datastore_id}"
    )


def import_from_gcs(project_id: str, datastore_id: str, bucket_name: str, gcs_prefix: str):
    """Import documents from GCS into a Vertex AI Search datastore.

    Args:
        project_id: GCP project ID.
        datastore_id: Target datastore ID.
        bucket_name: GCS bucket containing the docs.
        gcs_prefix: Prefix path in the bucket.
    """
    client = discoveryengine.DocumentServiceClient(
        client_options=_client_options(project_id)
    )

    parent = client.branch_path(
        project=project_id,
        location=LOCATION,
        data_store=datastore_id,
        branch="default_branch",
    )

    gcs_uri = f"gs://{bucket_name}/{gcs_prefix}/**"

    request = discoveryengine.ImportDocumentsRequest(
        parent=parent,
        gcs_source=discoveryengine.GcsSource(
            input_uris=[gcs_uri],
            data_schema="content",
        ),
        reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
    )

    print(f"Importing documents from {gcs_uri}...")
    operation = client.import_documents(request=request)
    print("Waiting for import to complete (this may take several minutes)...")
    response = operation.result(timeout=1800)
    print("Import completed.")

    if hasattr(response, "error_samples") and response.error_samples:
        print(f"  Errors: {len(response.error_samples)}")
        for err in response.error_samples[:5]:
            print(f"    {err}")


def main():
    parser = argparse.ArgumentParser(
        description="Upload docs to GCS and import into Vertex AI Search"
    )
    parser.add_argument("--project", default="development-459201", help="GCP project ID")
    parser.add_argument(
        "--datastore-id", default="csharp-sample-datastore", help="Datastore ID"
    )
    parser.add_argument(
        "--docs-dir", default="output_docs_pipeline", help="Path to generated docs"
    )
    parser.add_argument(
        "--gcs-bucket", default="codedoc-pipeline", help="GCS bucket name"
    )
    parser.add_argument(
        "--gcs-prefix", default="csharp-sample", help="GCS path prefix"
    )
    parser.add_argument(
        "--skip-create", action="store_true", help="Skip datastore creation"
    )
    parser.add_argument(
        "--skip-upload", action="store_true", help="Skip GCS upload"
    )
    args = parser.parse_args()

    if not args.skip_upload:
        if not os.path.isdir(args.docs_dir):
            print(f"Error: docs directory not found: {args.docs_dir}")
            return
        print("\nStep 1: Uploading docs to GCS...")
        upload_to_gcs(args.project, args.gcs_bucket, args.gcs_prefix, args.docs_dir)
    else:
        print("Step 1: Skipping GCS upload (--skip-upload)")

    full_datastore_path = (
        f"projects/{args.project}/locations/{LOCATION}"
        f"/collections/{COLLECTION}/dataStores/{args.datastore_id}"
    )

    if not args.skip_create:
        print("\nStep 2: Creating datastore...")
        try:
            full_datastore_path = create_datastore(
                args.project, args.datastore_id, "C# Sample Documentation"
            )
        except Exception as e:
            if "already exists" in str(e).lower():
                print(f"Datastore '{args.datastore_id}' already exists, reusing.")
            else:
                raise
    else:
        print("Step 2: Skipping datastore creation (--skip-create)")

    print("\nStep 3: Importing from GCS into datastore...")
    import_from_gcs(args.project, args.datastore_id, args.gcs_bucket, args.gcs_prefix)

    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nDatastore path for graph_query_agent/.env:")
    print(f"  VERTEX_SEARCH_DATASTORE={full_datastore_path}")
    print(f"\nUpdate graph_query_agent/.env with this value, then run:")
    print("  adk run graph_query_agent")


if __name__ == "__main__":
    main()
