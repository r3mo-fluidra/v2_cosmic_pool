# Notes before writing AWS infra (CDK/Terraform)

The app and CI/Docker baseline are ready. Two things need a decision before
infra is written, because they determine whether the app can run as more
than one container:

## 1. LangGraph checkpointer

`src/agent/graph.py` picks the checkpointer backend from `MARLIN_CHECKPOINTER`:

- `memory` (default): in-process, lost on restart. Fine for one container,
  breaks conversation continuity on every deploy/scale event.
- `sqlite`: a local file (`.marlin/checkpoints.sqlite`). Survives a restart
  but is single-writer — does not work across replicas (ECS tasks, Lambda
  concurrency, etc.), per the docstring on `_default_checkpointer`.

Production needs a shared backend (Postgres or DynamoDB — LangGraph ships
checkpointer implementations for both). This is implementation work, not
configuration: whoever writes the infra needs to also add that checkpointer
class to `graph.py`.

## 2. Qdrant vector store

Without `QDRANT_URL`, `src/qdrant_vector_store.py` falls back to an embedded
index on local disk, which — like the sqlite checkpointer — takes an
exclusive file lock and does not survive more than one worker (this was
already called out in `readme.md`). That embedded index was also being
committed to git as a binary; it has been removed from version control (see
`.gitignore`) but is still built locally on first run from
`src/data/documents/semantic_search/*.csv`.

For any deployment with more than one container, `QDRANT_URL` must point to
a real Qdrant instance (managed Qdrant Cloud, or self-hosted).

## Everything else

- `Dockerfile` / `.dockerignore` at the repo root build a runnable image
  (`streamlit run app.py` on port 8501, with a `/_stcore/health` healthcheck).
  CI builds it on every push (`docker-build` job) but does not push anywhere.
- Runtime env vars the container needs are the same ones in `.env.example`,
  plus `AWS_PROFILE`/`AWS_REGION` and the AgentCore Memory variables already
  used by `src/agent/memory_writer.py` (`MARLIN_MEMORY_ID`,
  `MARLIN_STRATEGY_PREFS`, `MARLIN_STRATEGY_VESSEL_FACTS`) — in AWS these
  should come from Secrets Manager / SSM Parameter Store per environment
  (dev/test/prod), not from a committed `.env.local` like today.
