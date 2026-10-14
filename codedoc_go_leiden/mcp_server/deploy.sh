#!/usr/bin/env bash
#
# Deploy the CodeDoc MCP server to Google Cloud Run.
#
# Usage (from the repository root):
#   ./mcp_server/deploy.sh
#
# Settings are env vars. The six keys the pipeline also uses (GOOGLE_CLOUD_PROJECT,
# GOOGLE_CLOUD_LOCATION, GEMINI_MODEL, SPANNER_INSTANCE, SPANNER_DATABASE, GRAPH_NAME)
# are additionally read from the repository-root .env when not exported. Everything
# else must be exported; the fallbacks are listed here.
#
#   PROJECT_ID / GOOGLE_CLOUD_PROJECT  GCP project (env, .env, then gcloud's active project)
#   SPANNER_INSTANCE          env, .env, fallback codedoc-instance (the pipeline's default)
#   SPANNER_DATABASE          env, .env, fallback codedoc-go-leiden (the pipeline's default) [LEIDEN-HOOK]
#   GRAPH_NAME                env, .env, fallback code_graph_a
#   GOOGLE_CLOUD_LOCATION, GEMINI_MODEL   env or .env; forwarded to the service only when set
#   REGION                    Cloud Run + Artifact Registry region (fallback: asia-northeast1)
#   SERVICE_NAME              Cloud Run service (fallback: codedoc-go-leiden-mcp) [LEIDEN-HOOK]
#   SERVICE_ACCOUNT_NAME      runtime service-account short name (fallback: codedoc-mcp-sa)
#   AR_REPO                   Artifact Registry Docker repository (fallback: codedoc)
#   FUNCTIONAL_TYPE           mcp-server (fallback) registers the service in Agent Registry via the
#                             Cloud Run Agent Platform features (Preview, needs `gcloud beta`).
#                             Set to "" to deploy a plain Cloud Run service instead.
#   IDENTITY_TYPE             service-account (fallback) | agent-identity. Used only with FUNCTIONAL_TYPE.
#                             Both properties are immutable once set on a service.
#   IAP                       1 enables Identity-Aware Proxy, 0 (fallback) disables it on each run.
#   MIN_INSTANCES, MAX_INSTANCES, MEMORY, CPU, TIMEOUT, CONCURRENCY   Cloud Run sizing.
#
# Idempotent: re-running upgrades the service and re-binds roles without harm. Note that
# --set-env-vars replaces the service's whole env-var set on every run.

set -euo pipefail

# --- preflight --------------------------------------------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud not found on PATH. Install the Google Cloud CLI (583 or newer) and run 'gcloud components update'." >&2
  exit 1
fi
if [[ ! -f mcp_server/Dockerfile ]]; then
  echo "ERROR: run this script from the repository root (mcp_server/Dockerfile not found at $(pwd)/mcp_server/Dockerfile)" >&2
  exit 1
fi

# dotenv KEY: value of KEY in the repo-root .env (last one wins; "export" prefix, CRLF,
# quotes and trailing comments tolerated). Mirrors what python-dotenv accepts for simple files.
dotenv() {
  [[ -f .env ]] || return 0
  tr -d '\r' < .env \
    | sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" | tail -n 1 \
    | sed -E -e 's/[[:space:]]+#.*$//' -e 's/^"(.*)"$/\1/' -e "s/^'(.*)'$/\1/" -e 's/[[:space:]]*$//'
}

# --- config -----------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(dotenv GOOGLE_CLOUD_PROJECT)}}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: no project. Set PROJECT_ID or GOOGLE_CLOUD_PROJECT (env or .env), or run 'gcloud config set project <id>'." >&2
  exit 1
fi
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-codedoc-go-leiden-mcp}"  # LEIDEN-HOOK
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-codedoc-mcp-sa}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="${AR_REPO:-codedoc}"

SPANNER_INSTANCE="${SPANNER_INSTANCE:-$(dotenv SPANNER_INSTANCE)}"
SPANNER_INSTANCE="${SPANNER_INSTANCE:-codedoc-instance}"
SPANNER_DATABASE="${SPANNER_DATABASE:-$(dotenv SPANNER_DATABASE)}"
SPANNER_DATABASE="${SPANNER_DATABASE:-codedoc-go-leiden}"  # LEIDEN-HOOK
GRAPH_NAME="${GRAPH_NAME:-$(dotenv GRAPH_NAME)}"
GRAPH_NAME="${GRAPH_NAME:-code_graph_a}"
GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-$(dotenv GOOGLE_CLOUD_LOCATION)}"
GEMINI_MODEL="${GEMINI_MODEL:-$(dotenv GEMINI_MODEL)}"

FUNCTIONAL_TYPE="${FUNCTIONAL_TYPE-mcp-server}"   # "${VAR-x}" on purpose: an explicitly empty value disables registration
IDENTITY_TYPE="${IDENTITY_TYPE:-service-account}"
IAP="${IAP:-0}"

MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-2}"
TIMEOUT="${TIMEOUT:-300}"
CONCURRENCY="${CONCURRENCY:-10}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-5}"

case "${FUNCTIONAL_TYPE}" in
  ""|mcp-server) ;;
  *) echo "ERROR: FUNCTIONAL_TYPE must be 'mcp-server' or empty (got '${FUNCTIONAL_TYPE}')" >&2; exit 1 ;;
esac
case "${IDENTITY_TYPE}" in
  service-account|agent-identity) ;;
  *) echo "ERROR: IDENTITY_TYPE must be 'service-account' or 'agent-identity' (got '${IDENTITY_TYPE}')" >&2; exit 1 ;;
esac
case "${IAP}" in
  0|1) ;;
  *) echo "ERROR: IAP must be 0 or 1 (got '${IAP}')" >&2; exit 1 ;;
esac
if [[ -z "${FUNCTIONAL_TYPE}" && "${IDENTITY_TYPE}" != "service-account" ]]; then
  echo "NOTE: IDENTITY_TYPE=${IDENTITY_TYPE} needs FUNCTIONAL_TYPE; using service-account for a plain deploy."
  IDENTITY_TYPE="service-account"
fi

# Container Registry (gcr.io) was shut down on 2025-03-18; images live in Artifact Registry.
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/codedoc-go-leiden-mcp"  # LEIDEN-HOOK
# Cloud Build only auto-populates SHORT_SHA for triggered builds, so compute it here.
SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
IMAGE="${IMAGE_REPO}:${SHORT_SHA}"

# Env vars injected into the service. .env itself is never uploaded or copied.
SERVICE_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}##GOOGLE_GENAI_USE_VERTEXAI=true##SPANNER_INSTANCE=${SPANNER_INSTANCE}##SPANNER_DATABASE=${SPANNER_DATABASE}##GRAPH_NAME=${GRAPH_NAME}"
if [[ -n "${GOOGLE_CLOUD_LOCATION}" ]]; then SERVICE_ENV+="##GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION}"; fi
if [[ -n "${GEMINI_MODEL}" ]]; then SERVICE_ENV+="##GEMINI_MODEL=${GEMINI_MODEL}"; fi

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  echo "ERROR: no active gcloud account. Run 'gcloud auth login' first." >&2
  exit 1
fi
if [[ "${ACTIVE_ACCOUNT}" == *.gserviceaccount.com ]]; then
  ACTIVE_MEMBER="serviceAccount:${ACTIVE_ACCOUNT}"
else
  ACTIVE_MEMBER="user:${ACTIVE_ACCOUNT}"
fi

echo "==> Deploying ${SERVICE_NAME}"
echo "    project:         ${PROJECT_ID}"
echo "    region:          ${REGION}"
echo "    image:           ${IMAGE}"
echo "    spanner:         ${SPANNER_INSTANCE} / ${SPANNER_DATABASE} / ${GRAPH_NAME}"
echo "    functional type: ${FUNCTIONAL_TYPE:-(none: plain Cloud Run service)}"
echo "    identity:        ${IDENTITY_TYPE}$([[ "${IDENTITY_TYPE}" == service-account ]] && echo " (${SERVICE_ACCOUNT_EMAIL})")"
echo "    IAP:             ${IAP}"
echo "    active gcloud:   ${ACTIVE_ACCOUNT}"

# --- enable required APIs (idempotent) --------------------------------------
APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
      iam.googleapis.com aiplatform.googleapis.com spanner.googleapis.com)
if [[ -n "${FUNCTIONAL_TYPE}" ]]; then
  APIS+=(agentregistry.googleapis.com apphub.googleapis.com)
fi
if [[ "${IAP}" == "1" ]]; then
  APIS+=(iap.googleapis.com)
fi
echo "==> Ensuring required APIs are enabled: ${APIS[*]}"
gcloud services enable "${APIS[@]}" --project "${PROJECT_ID}" --quiet

# --- Artifact Registry repository (idempotent) ------------------------------
if ! gcloud artifacts repositories describe "${AR_REPO}" \
       --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1; then
  echo "==> Creating Artifact Registry repository ${REGION}/${AR_REPO}"
  gcloud artifacts repositories create "${AR_REPO}" \
    --project "${PROJECT_ID}" --location "${REGION}" \
    --repository-format docker --description "CodeDoc container images" --quiet
else
  echo "==> Artifact Registry repository ${REGION}/${AR_REPO} already exists"
fi

# --- runtime service account (only for IDENTITY_TYPE=service-account) --------
if [[ "${IDENTITY_TYPE}" == "service-account" ]]; then
  if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT_EMAIL}" \
         --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "==> Creating service account ${SERVICE_ACCOUNT_EMAIL}"
    gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
      --project "${PROJECT_ID}" \
      --display-name "CodeDoc MCP server"
  else
    echo "==> Service account ${SERVICE_ACCOUNT_EMAIL} already exists"
  fi
  echo "==> Binding runtime roles to ${SERVICE_ACCOUNT_EMAIL}"
  for role in roles/aiplatform.user roles/spanner.databaseUser; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member "serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
      --role "${role}" \
      --condition=None \
      --quiet >/dev/null
    echo "    bound ${role}"
  done
fi

# --- build ------------------------------------------------------------------
# mcp_server/.gcloudignore restricts the upload to mcp_server/ and graph_query_agent/
# (no .env, no .venv, no node_modules, no test corpora).
echo "==> Building image via Cloud Build (tags: latest, ${SHORT_SHA})"
gcloud builds submit . \
  --project "${PROJECT_ID}" \
  --config mcp_server/cloudbuild.yaml \
  --ignore-file mcp_server/.gcloudignore \
  --substitutions "_IMAGE=${IMAGE_REPO},SHORT_SHA=${SHORT_SHA}"

# --- deploy -----------------------------------------------------------------
DEPLOY_CMD=(gcloud run deploy)
EXTRA_FLAGS=()
if [[ -n "${FUNCTIONAL_TYPE}" ]]; then
  # Agent Platform features are Preview and live in the beta track.
  DEPLOY_CMD=(gcloud beta run deploy)
  EXTRA_FLAGS+=(--functional-type "${FUNCTIONAL_TYPE}" --identity-type "${IDENTITY_TYPE}")
fi
if [[ "${IDENTITY_TYPE}" == "service-account" ]]; then
  EXTRA_FLAGS+=(--service-account "${SERVICE_ACCOUNT_EMAIL}")
fi
if [[ "${IAP}" == "1" ]]; then
  EXTRA_FLAGS+=(--iap)
else
  EXTRA_FLAGS+=(--no-iap)   # keeps re-runs idempotent when IAP was enabled earlier
fi

echo "==> Deploying to Cloud Run (${DEPLOY_CMD[*]})"
"${DEPLOY_CMD[@]}" "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --no-allow-unauthenticated \
  --session-affinity \
  --memory "${MEMORY}" \
  --cpu "${CPU}" \
  --timeout "${TIMEOUT}" \
  --concurrency "${CONCURRENCY}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  "${EXTRA_FLAGS[@]}" \
  --set-env-vars "^##^${SERVICE_ENV}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)')"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

# --- post-deploy IAM ---------------------------------------------------------
if [[ "${IAP}" == "1" ]]; then
  # IAP fronts the service and must itself be allowed to invoke it. The IAP service agent
  # only exists once something has provisioned it, so create it first (idempotent).
  echo "==> Granting the IAP service agent roles/run.invoker on ${SERVICE_NAME}"
  gcloud beta services identity create --service iap.googleapis.com --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" --region "${REGION}" \
    --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
    --role roles/run.invoker --quiet >/dev/null
fi

if [[ "${IDENTITY_TYPE}" == "agent-identity" ]]; then
  # A system-managed agent identity is a new principal; it inherits nothing from any service account.
  ORG_ID="$(gcloud projects get-ancestors "${PROJECT_ID}" --format='csv[no-heading](id,type)' 2>/dev/null \
            | awk -F, '$2=="organization"{print $1; exit}')"
  if [[ -n "${ORG_ID}" ]]; then
    AGENT_PRINCIPAL="principal://agents.global.org-${ORG_ID}.system.id.goog/resources/run/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE_NAME}"
    echo "==> Granting runtime roles to the agent identity"
    for role in roles/aiplatform.user roles/spanner.databaseUser; do
      if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
           --member "${AGENT_PRINCIPAL}" --role "${role}" --condition=None --quiet >/dev/null; then
        echo "    bound ${role}"
      else
        echo "    WARN: could not bind ${role} to ${AGENT_PRINCIPAL}. Grant it manually:"
        echo "      gcloud projects add-iam-policy-binding ${PROJECT_ID} --member '<identity from the revision Security tab>' --role ${role}"
      fi
    done
  else
    cat <<EOF
WARN: project ${PROJECT_ID} has no organization ancestor, so the agent principal could not be derived.
      Read the identity from the revision (Cloud Run console > revision > Security > Identity, or
      'gcloud beta run revisions describe <revision> --project ${PROJECT_ID} --region ${REGION}') and grant it:
        gcloud projects add-iam-policy-binding ${PROJECT_ID} --member '<identity>' --role roles/aiplatform.user
        gcloud projects add-iam-policy-binding ${PROJECT_ID} --member '<identity>' --role roles/spanner.databaseUser
EOF
  fi
fi

# --- post-deploy instructions -----------------------------------------------
cat <<EOF

==============================================================================
Deploy complete.

  Service:   ${SERVICE_NAME}
  URL:       ${SERVICE_URL}
  Image:     ${IMAGE}
  Auth:      $([[ "${IAP}" == "1" ]] && echo "Identity-Aware Proxy" || echo "Cloud Run IAM (no public access)")
  Registry:  $([[ -n "${FUNCTIONAL_TYPE}" ]] && echo "registered in Agent Registry as ${FUNCTIONAL_TYPE} (identity: ${IDENTITY_TYPE})" || echo "no registration requested (an earlier registration on this service stays; the property is immutable)")
EOF

if [[ "${IAP}" == "1" ]]; then
cat <<EOF

Grant each user access through IAP (one-time, per principal):

  gcloud iap web add-iam-policy-binding \\
    --project ${PROJECT_ID} \\
    --region ${REGION} \\
    --resource-type cloud-run \\
    --service ${SERVICE_NAME} \\
    --member ${ACTIVE_MEMBER} \\
    --role roles/iap.httpsResourceAccessor

Non-browser MCP clients also need a custom OAuth client allow-listed for IAP
programmatic access; see mcp_server/README.md ("Authentication options").
EOF
else
cat <<EOF

Grant yourself the Cloud Run Invoker role (one-time, per principal):

  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\
    --project ${PROJECT_ID} \\
    --region ${REGION} \\
    --member ${ACTIVE_MEMBER} \\
    --role roles/run.invoker

Then expose it to your local MCP client via the gcloud proxy:

  gcloud run services proxy ${SERVICE_NAME} \\
    --project ${PROJECT_ID} \\
    --region ${REGION} \\
    --port 8080

...and add this to your .mcp.json (the URL is the same as the local-dev URL):

  {
    "mcpServers": {
      "codedoc": {
        "type": "http",
        "url": "http://127.0.0.1:8080/mcp"
      }
    }
  }
EOF
fi

if [[ -n "${FUNCTIONAL_TYPE}" ]]; then
cat <<EOF

Inspect the registered identity:
  gcloud beta run revisions describe \$(gcloud run services describe ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --format='value(status.latestReadyRevisionName)') --project ${PROJECT_ID} --region ${REGION}
EOF
fi
echo "=============================================================================="
