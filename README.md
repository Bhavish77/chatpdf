# AskDocs

A web app for asking questions about your own documents, with per-user auth, an async ingestion
queue, and a self-grading LangGraph RAG chat backend over free Gemini models.

Work in progress, built phase by phase per `BUILD_SPEC.md`. This section will be replaced with the
full README (pitch, screenshots, architecture, quickstart, deploy guide, config, limitations, eval
results) in Phase 7.

## Local quickstart (current phase)

```
cp .env.example .env
docker compose up --build
curl http://localhost:8010/api/health
```
