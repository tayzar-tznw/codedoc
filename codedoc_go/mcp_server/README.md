# CodeDoc MCP server (Go variant)

An [MCP](https://modelcontextprotocol.io) server built on [FastMCP 2.x](https://gofastmcp.com) that exposes the CodeDoc query agent (`graph_query_agent`) to any MCP-compatible host — **Antigravity CLI (`agy`)**, Claude Code, [MCP Inspector](https://github.com/modelcontextprotocol/inspector), etc.

Runs over **streamable-http** transport. Two ways to use it:

1. **Local**: `python -m mcp_server` → server on `http://127.0.0.1:8080/mcp`.
2. **Cloud Run**: deploy with `./mcp_server/deploy.sh`, then `gcloud run services proxy …` exposes the remote service on the same `http://127.0.0.1:8080/mcp` URL — your MCP client config works for both modes unchanged.

The server is a thin wrapper around the same ADK Runner pattern used by `webapp/main.py`. It does **not** spawn the webapp; it imports the agent directly and runs it in-process. This copy belongs to `codedoc_go`, the CodeDoc variant for Go modules; its defaults (service and image `codedoc-go-mcp`, database `codedoc-go`) differ from the sibling variants' so that deploying one never replaces another.

---

## Tools

| Tool | Purpose |
|---|---|
| `ask_codebase(question, session_id?)` | Main Q&A, backed by `graph_query_agent`. Returns `{response, session_id, turn_count}`. Pass `session_id` from a prior call to ask a follow-up in the same thread. |
| `list_sessions()` | Enumerate active sessions, most recently updated first. |
| `get_session_history(session_id)` | Full transcript as a list of `{role, text, timestamp}` turns. |
| `delete_session(session_id)` | Idempotent prune. |
| `rename_session(session_id, label)` | Attach a human-readable label. |

`graph_query_agent`'s orchestrator delegates to its graph specialist (`graph_agent`) for structural questions — package, type and function listings, call chains, interface implementations, embedding, package dependencies, calls across modules and repositories, impact analysis, counts. Documentation lookups are served straight from the Spanner code graph: the node tables (Files/Packages/Classes/Methods/Topics/Directories) carry `summary` columns alongside the structural fields, so a single GQL query covers both.

---

## Pre-setup (one-time per GCP project)

The MCP server is a thin transport — it does NOT provision its own backing services. **Cloud Spanner Graph is the only required backing service**: the graph specialist runs GQL against `SPANNER_INSTANCE` / `SPANNER_DATABASE` / `GRAPH_NAME`. A missing Spanner instance shows up at first tool call as `NOT_FOUND: Instance not found`.

The pipeline CLI creates it, reading the same repo-root `.env` that the agent and `deploy.sh` read:

```bash
# Spanner Graph: instance (100 PU, Enterprise edition, billed while it exists) + database + tables + property graph
python -m graph_generator setup spanner

# Tear down Spanner when done (asks for confirmation):
python -m graph_generator.setup_spanner_graph --destroy
```

`GOOGLE_CLOUD_PROJECT`, `SPANNER_INSTANCE`, `SPANNER_DATABASE` and `GRAPH_NAME` in the repo-root `.env` are the single source of truth: the pipeline writes there, the agent queries there, and `deploy.sh` copies the same values into the Cloud Run service. Without `.env`, every component falls back to `codedoc-instance` / `codedoc-go` / `code_graph_a`. The root `.env` is the only one read: there is no `graph_query_agent/.env` or `mcp_server/.env`.

---

## Local development

### Prerequisites

1. Project venv: from the repo root, `uv pip install --python .venv/bin/python -r requirements.txt -r mcp_server/requirements.txt` (there is no `pyproject.toml`/`uv.lock`; `fastmcp>=2.0,<3` lives in `mcp_server/requirements.txt`, not in the root `requirements.txt`).
2. GCP credentials: `gcloud auth application-default login` (the local server uses your user creds). Set a quota project: `gcloud auth application-default set-quota-project <your GOOGLE_CLOUD_PROJECT>`.
3. Env vars (in the repo-root `.env`, loaded by `mcp_server/runner.py` and `graph_query_agent/config.py`). What the agent actually reads:
   - `GOOGLE_CLOUD_PROJECT` — the project used for Spanner and the Vertex SDK. There is no hardcoded default: when it is unset, the Google client libraries use the ADC / gcloud default project, and the agent prints the project it uses at startup (`graph_query_agent/config.py`); `deploy.sh` falls back to gcloud's active project.
   - `GOOGLE_GENAI_USE_VERTEXAI` — set to `true` so the Gemini client routes through Vertex AI.
   - `GOOGLE_CLOUD_LOCATION` (Vertex AI location; the Gen AI SDK falls back to `global`) and `GEMINI_MODEL` (the agent's model; `graph_query_agent/config.py` falls back to `gemini-3.5-flash`). `deploy.sh` forwards both when set.
   - `SPANNER_INSTANCE` / `SPANNER_DATABASE` / `GRAPH_NAME` — Spanner Graph. Fallbacks `codedoc-instance` / `codedoc-go` / `code_graph_a` everywhere, including `deploy.sh`.

### Launch

```bash
python -m mcp_server
```

Server starts on `http://0.0.0.0:8080/mcp`. Set `HOST`/`PORT` to override.

### Hook into Antigravity CLI (`agy`)

`agy` discovers MCP servers via **extensions** at `~/.gemini/extensions/<name>/gemini-extension.json` (NOT via a project-local `.mcp.json` — agy doesn't honor that path). Create the file:

```bash
mkdir -p ~/.gemini/extensions/codedoc
cat > ~/.gemini/extensions/codedoc/gemini-extension.json <<'EOF'
{
  "name": "codedoc",
  "version": "1.0.0",
  "description": "Query the CodeDoc-indexed codebase via the MCP server.",
  "mcpServers": {
    "codedoc": {
      "httpUrl": "http://127.0.0.1:8080/mcp",
      "timeout": 120000
    }
  }
}
EOF
```

Restart `agy`; the codedoc tools become available to any prompt. The same config works for Cloud Run mode — `gcloud run services proxy` makes the remote service look local on the same URL.

### Hook into Claude Code

`.mcp.json` (project root) or `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "codedoc": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

Restart Claude Code; `/mcp` should list `codedoc` as connected.

### Verify

```bash
npx @modelcontextprotocol/inspector http://127.0.0.1:8080/mcp
```

Or with curl:

```bash
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Expect a JSON-RPC `result` with `serverInfo.name = "codedoc"`.

End-to-end test with agy (after the extension is installed):

```bash
agy --print 'Use the codedoc.ask_codebase tool to ask: 何のサンプルコードですか？' \
  --dangerously-skip-permissions --print-timeout 120s
```

---

## Deploy to Cloud Run

The server ships with everything needed to run as a private Cloud Run service. By default it is deployed through the **Cloud Run Agent Platform features** (Preview): `gcloud beta run deploy --functional-type=mcp-server` registers the service automatically in your organization's Agent Registry under `/mcpServers`. Callers authenticate with Cloud Run IAM and connect through `gcloud run services proxy`; Identity-Aware Proxy can be switched on instead. Reference: [Configure Agent Platform features for Cloud Run](https://docs.cloud.google.com/run/docs/ai/agent-platform-features), [Host MCP servers on Cloud Run](https://docs.cloud.google.com/run/docs/host-mcp-servers), [Authenticate MCP servers](https://docs.cloud.google.com/run/docs/ai/authenticate-mcp-servers).

### Prerequisites

- `gcloud` 583 or newer with the `beta` component (`gcloud components update`), authenticated with `gcloud auth login`.
- A GCP project with billing enabled. The deployer needs `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser` (to deploy as the runtime service account), `roles/resourcemanager.projectIamAdmin` (the script binds project-level roles), `roles/serviceusage.serviceUsageAdmin`, `roles/cloudbuild.builds.editor`, `roles/artifactregistry.admin`, and, for Agent Registry registration, `roles/apphub.admin` or `roles/apiregistry.admin`. `IAP=1` additionally needs `roles/iap.admin`. `roles/owner` covers all of these for testing. The Cloud Build service account must be able to push to Artifact Registry and write logs (`roles/cloudbuild.builds.builder` includes both).
- [Agent Registry set up](https://docs.cloud.google.com/agent-registry/setup) in the project or organization. Not needed when you deploy with `FUNCTIONAL_TYPE=""`.
- The Spanner instance and database referenced by `SPANNER_INSTANCE` / `SPANNER_DATABASE` must already exist (see Pre-setup).

### One-shot deploy

From the **project root** (not from inside `mcp_server/`). `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL`, `SPANNER_INSTANCE`, `SPANNER_DATABASE` and `GRAPH_NAME` are read from exported env vars first and from the repo-root `.env` second. Every other setting (`REGION`, `SERVICE_NAME`, `AR_REPO`, `FUNCTIONAL_TYPE`, `IDENTITY_TYPE`, `IAP`, sizing) must be exported; values placed in `.env` are ignored.

```bash
export PROJECT_ID="$(gcloud config get-value project)"   # or GOOGLE_CLOUD_PROJECT in .env
export REGION="asia-northeast1"
export SERVICE_NAME="codedoc-go-mcp"
export AR_REPO="codedoc"                  # Artifact Registry Docker repository (created if missing)
export SPANNER_INSTANCE="codedoc-instance"           # usually already in .env
export SPANNER_DATABASE="codedoc-go"
export GRAPH_NAME="code_graph_a"
export FUNCTIONAL_TYPE="mcp-server"       # "" = plain Cloud Run service, no Agent Registry entry
export IDENTITY_TYPE="service-account"    # or agent-identity
export IAP=0                              # 1 = front the service with Identity-Aware Proxy

./mcp_server/deploy.sh
```

Defaults when a value is not set:

| Setting | Default |
|---|---|
| `SERVICE_NAME` | `codedoc-go-mcp` |
| Image | `REGION-docker.pkg.dev/PROJECT_ID/AR_REPO/codedoc-go-mcp` |
| `AR_REPO` | `codedoc` |
| `SERVICE_ACCOUNT_NAME` | `codedoc-mcp-sa` (shared with the sibling CodeDoc variants; the roles it gets are the same) |
| `SPANNER_INSTANCE` / `SPANNER_DATABASE` / `GRAPH_NAME` | `codedoc-instance` / `codedoc-go` / `code_graph_a` |
| `REGION` | `asia-northeast1` |

The script is idempotent. On each run it:

1. Enables the APIs: Cloud Run, Cloud Build, Artifact Registry, IAM, Vertex AI, Spanner; plus Agent Registry and App Hub when registering; plus IAP when `IAP=1`.
2. Creates (or reuses) the Artifact Registry repository `REGION-docker.pkg.dev/PROJECT_ID/AR_REPO`. Container Registry (`gcr.io`) was shut down in March 2025, so images must live here.
3. With `IDENTITY_TYPE=service-account`: creates (or reuses) `codedoc-mcp-sa@PROJECT_ID.iam.gserviceaccount.com` and binds `roles/aiplatform.user` (Gemini and embeddings) and `roles/spanner.databaseUser` (GQL queries).
4. Builds the image `codedoc-go-mcp` with Cloud Build from `mcp_server/Dockerfile`, tagged `latest` and the git short SHA; the service runs the SHA tag. The upload is restricted by `mcp_server/.gcloudignore` to `mcp_server/` and `graph_query_agent/` (no `.env` files, no `.venv`, no `node_modules`, no test corpora), and `.dockerignore` excludes `**/.env` from the image.
5. Deploys with `--no-allow-unauthenticated`, `--session-affinity` and the sizing 2 Gi / 2 CPU / 0–5 instances / 10 concurrent / 300 s. Unless `FUNCTIONAL_TYPE=""`, the command is `gcloud beta run deploy --functional-type=mcp-server --identity-type=...`.
6. Sets the service env vars `GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_VERTEXAI=true`, `SPANNER_INSTANCE`, `SPANNER_DATABASE`, `GRAPH_NAME`, plus `GOOGLE_CLOUD_LOCATION` and `GEMINI_MODEL` when they are set locally. `--set-env-vars` replaces the service's whole env-var set on every run, so anything added by hand in the console is dropped. No `.env` file reaches the image.
7. With `IAP=1`: provisions the IAP service agent if needed and grants it `roles/run.invoker` on the service. With `IAP=0`: passes `--no-iap`, so a previously enabled IAP is switched off again. With `IDENTITY_TYPE=agent-identity`: grants the runtime roles to the agent principal, or prints the manual `add-iam-policy-binding` commands if the organization ID cannot be resolved.
8. Prints the service URL and the follow-up commands.

`--functional-type` and `--identity-type` cannot be changed or cleared once set on a service. Re-running with `FUNCTIONAL_TYPE=""` leaves an existing registration in place; to switch, deploy under a new `SERVICE_NAME` or delete the service first.

### Authentication options

| Option | When to use it | What you do |
|---|---|---|
| Cloud Run IAM + `gcloud run services proxy` (default, `IAP=0`) | Developers who have `gcloud` | Grant `roles/run.invoker`, run the proxy, point the MCP client at `http://127.0.0.1:8080/mcp` |
| Identity-Aware Proxy (`IAP=1`) | Google's recommendation for CLI tools and generic MCP clients that talk to a private MCP server directly | Grant `roles/iap.httpsResourceAccessor`. Non-browser clients also need a custom OAuth client allow-listed for IAP programmatic access, and an `oauth` block (`clientId`, `clientSecret`, `scopes: email, openid`, `redirectUri`) in the client's `mcpServers` config |
| Agent Identity (`IDENTITY_TYPE=agent-identity`) | Agents inside the Agent Platform calling this server | The service gets a system-managed identity. It is a new principal that inherits nothing from the service account, so the runtime roles must be granted to it |

IAP commands, if you did not let `deploy.sh` do it:

```bash
gcloud run services update codedoc-go-mcp --project "$PROJECT_ID" --region "$REGION" --iap
gcloud iap web add-iam-policy-binding \
  --project "$PROJECT_ID" --region "$REGION" \
  --resource-type cloud-run --service codedoc-go-mcp \
  --member user:you@example.com --role roles/iap.httpsResourceAccessor
```

### Grant yourself invoker access (default IAM mode)

Each principal that should reach the MCP server needs `roles/run.invoker` on the service. The deploy script prints the exact command — run it once per teammate:

```bash
gcloud run services add-iam-policy-binding codedoc-go-mcp \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --member user:you@example.com \
  --role roles/run.invoker
```

### Connect your MCP client to the remote server

`gcloud` proxies the authenticated remote service onto a local port using your own credentials. No tokens to copy.

```bash
gcloud run services proxy codedoc-go-mcp \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --port 8080
```

Leave that terminal running. Your existing agy extension / `.mcp.json` works unchanged — `http://127.0.0.1:8080/mcp` now routes through the proxy to the Cloud Run service.

### Check the Agent Registry entry

The service appears under `/mcpServers` in Agent Registry. The identity assigned to a revision is shown in the Cloud Run console (revision → Security tab → Identity) or with:

```bash
gcloud beta run revisions describe REVISION_NAME --project "$PROJECT_ID" --region "$REGION"
```

### Tearing down

```bash
gcloud run services delete codedoc-go-mcp --project "$PROJECT_ID" --region "$REGION"
# Optional: the runtime service account and the image repository (both are shared with the sibling
# CodeDoc variants; delete them only when no other variant's service uses them)
gcloud iam service-accounts delete "codedoc-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com" --project "$PROJECT_ID"
gcloud artifacts repositories delete codedoc --project "$PROJECT_ID" --location "$REGION"
```

After deleting the service, check Agent Registry for a leftover `/mcpServers` entry.

---

## Known caveats

- **Sessions are in-memory per instance.** When Cloud Run scales an instance down or rolls one, the transcripts and labels for sessions on that instance vanish. `--session-affinity` keeps subsequent requests for the same `session_id` on the same instance while it lives, but it can't survive instance death. To make sessions durable across restarts, swap `InMemorySessionService` for `DatabaseSessionService(db_url="…")` in `mcp_server/runner.py` — one-line change — and back it with **Cloud SQL** (Postgres or MySQL) or ADK's **`VertexAiSessionService`**. Avoid SQLite-on-GCS-FUSE: the WAL/locking semantics break on object-storage-backed mounts.
- **Cold start.** ADK + Spanner client import takes several seconds. The first request after scale-to-zero will be slow. Set `MIN_INSTANCES=1` when invoking `deploy.sh` if you want a warm pool (~$0.024/hour idle).
- **No concurrency lock on the per-process metadata dict.** CPython's GIL keeps simple ops safe, but two simultaneous first-calls for the same `session_id` could race. Coding-CLI usage almost never triggers this.
- **CMEK / VPC connector not configured.** If your Spanner instance is behind a VPC, add `--vpc-connector` and `--vpc-egress` flags in `deploy.sh`. Out of scope for the default deploy.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ImportError: graph_query_agent` when running locally | You launched from a directory other than the repo root. `python -m mcp_server` expects the repo root on `sys.path`. |
| `ask_codebase` returns `NOT_FOUND: Instance not found` | Spanner instance referenced by `SPANNER_INSTANCE` doesn't exist. Run `python -m graph_generator.setup_spanner_graph --project ${GOOGLE_CLOUD_PROJECT} --instance ${SPANNER_INSTANCE} --database ${SPANNER_DATABASE}`. |
| `403 Forbidden` from the proxy | Your principal lacks `roles/run.invoker` on the service. Re-run the `add-iam-policy-binding` command above. |
| `PermissionDenied` from Spanner in the Cloud Run logs | Service-account role binding hasn't propagated yet (can take 60–90 s) or the wrong project was used. Check `gcloud projects get-iam-policy ${PROJECT_ID} --flatten='bindings[].members' --filter='bindings.members:codedoc-mcp-sa@'`. |
| agy doesn't see the codedoc tools | Extension is missing or in the wrong place. Confirm `ls ~/.gemini/extensions/codedoc/gemini-extension.json` exists and the JSON parses. Restart `agy`. |
| Cold-start timeout | Bump `--timeout` in `deploy.sh` or set `MIN_INSTANCES=1`. |
| Local server logs are noisy with `[EXPERIMENTAL] PLUGGABLE_AUTH` warning | Harmless ADK feature-flag warning. Sent to stderr only. |
| `unrecognized arguments: --functional-type` | The `beta` component is missing or older than 2026.08. Run `gcloud components update`, or deploy with `FUNCTIONAL_TYPE=""`. |
| Deploy fails with an Agent Registry / App Hub error | Agent Registry is not set up in the project or organization, or `agentregistry.googleapis.com` / `apphub.googleapis.com` are disabled. Set it up, or deploy with `FUNCTIONAL_TYPE=""`. |
| Error that `functional-type` or `identity-type` is immutable | You changed `FUNCTIONAL_TYPE` or `IDENTITY_TYPE` on an existing service. Deploy under a new `SERVICE_NAME` or delete the service first. |
| `403` after enabling IAP | The principal lacks `roles/iap.httpsResourceAccessor`, or a CLI client is using the Google-managed OAuth client, which only supports browser logins. Grant the role and configure a custom OAuth client. |
| Push to `gcr.io` fails | Container Registry is shut down. `deploy.sh` builds into Artifact Registry; if you build by hand, pass `_IMAGE=REGION-docker.pkg.dev/PROJECT/REPO/codedoc-go-mcp` to `cloudbuild.yaml`. |
