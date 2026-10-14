# Use official Python runtime as base
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY webapp/main.py ./webapp/
COPY webapp/graph_api.py ./webapp/
COPY webapp/frontend/dist ./webapp/frontend/dist
COPY output_docs_pipeline ./output_docs_pipeline
COPY graph_query_agent ./graph_query_agent

# Settings come from the container's env vars (Cloud Run: --set-env-vars); no .env
# is baked in. OUTPUT_DIR stays the pipeline root (output_docs_pipeline); the webapp
# serves OUTPUT_DIR/repos/<repo>/ for ?repo=<repo>, else WEBAPP_REPO=<repo>, else the
# only repo there. Set WEBAPP_REPO=<repo> at run time when several repos are copied in.
ENV GOOGLE_GENAI_USE_VERTEXAI=true

# Expose port
EXPOSE 8080

# Set environment variables
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Run the application
CMD cd /app && uvicorn webapp.main:app --host 0.0.0.0 --port $PORT
