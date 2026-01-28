# Hindsight Configuration: What We Set vs Defaults

This doc lists the main [Hindsight configuration](https://hindsight.vectorize.io/developer/configuration) variables, how we supply them, and what we don't set (Hindsight defaults).

---

## Summary

| Source | What it means |
|--------|----------------|
| **CDK hardcoded** | Value is fixed in `cdk/hindsight_stack.py` (or CDK defaults). Change requires code + redeploy. |
| **`cdk/config.py`** | Value comes from config; change config, redeploy. |
| **Secrets Manager** | Value stored in AWS Secrets Manager. ECS task reads it at runtime. |
| **GitHub Actions** | Workflow updates a secret (e.g. in Secrets Manager) during deploy. |
| **Hindsight default** | We never set it; Hindsight uses its built‑in default. |

---

## 1. Database

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_DATABASE_URL` | PostgreSQL connection string | **Secrets Manager** (`DbUrlSecret`). CDK creates the secret; **GitHub Actions** "Set Database URL Secret" step writes the value (constructed from RDS endpoint + password) after each deploy. ECS task reads it as a secret. |
| `HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP` | Run DB migrations on startup | **Hindsight default** (`true`). We don't set it. |
| `HINDSIGHT_API_DB_POOL_*` (min/max size, timeouts) | Connection pool settings | **Hindsight defaults**. We don't set them. |

---

## 2. LLM (retain / reflect)

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_LLM_PROVIDER` | Provider: openai, anthropic, groq, gemini, etc. | **`cdk/config.py`** → `LLM_PROVIDER`. CDK passes it into the ECS task as env. |
| `HINDSIGHT_API_LLM_MODEL` | Model name (e.g. gpt-4o-mini) | **`cdk/config.py`** → `LLM_MODEL`. CDK passes it into the ECS task as env. |
| `HINDSIGHT_API_LLM_API_KEY` | API key for the LLM provider | **Secrets Manager** (`LlmApiKeySecret`). **GitHub Actions** "Set LLM API key in Secrets Manager" step writes `secrets.LLM_API_KEY` into that secret on each deploy. ECS task reads it as a secret. |
| `HINDSIGHT_API_LLM_BASE_URL` | Custom LLM endpoint (e.g. Azure, local) | **Not set.** Hindsight uses provider default. |
| `HINDSIGHT_API_LLM_MAX_CONCURRENT`, `LLM_TIMEOUT`, `GROQ_SERVICE_TIER` | Concurrency, timeout, Groq tier | **Hindsight defaults.** We don't set them. |

**Per‑operation (retain/reflect) overrides** (`HINDSIGHT_API_RETAIN_LLM_*`, `HINDSIGHT_API_REFLECT_LLM_*`): **Not set.** Hindsight uses the main LLM config above.

---

## 3. Embeddings

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` | local, tei, openai, cohere, litellm | **Hindsight default** (`local`). We don't set it. |
| `HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL` | Model for local embeddings | **Hindsight default** (e.g. `BAAI/bge-small-en-v1.5`). We don't set it. |
| Others (TEI URL, OpenAI/Cohere keys, etc.) | For non‑local embeddings | **Not set.** We use local embeddings. |

---

## 4. Reranker

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_RERANKER_PROVIDER` | local, tei, cohere, etc. | **Hindsight default** (`local`). We don't set it. |
| `HINDSIGHT_API_RERANKER_LOCAL_MODEL` | Local reranker model | **Hindsight default.** We don't set it. |

---

## 5. Server

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_HOST` | Bind address | **Hindsight default** (`0.0.0.0`). We don't set it. |
| `HINDSIGHT_API_PORT` | Server port | **`cdk/config.py`** → `HINDSIGHT_API_PORT` (8888). CDK passes it into the ECS task as env. |
| `HINDSIGHT_API_WORKERS`, `LOG_LEVEL`, `MCP_*` | Workers, logging, MCP | **Hindsight defaults.** We don't set them. |

---

## 6. Authentication (Hindsight API)

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_TENANT_EXTENSION` | Enable API key auth | **Not set.** Hindsight runs without auth. |
| `HINDSIGHT_API_TENANT_API_KEY` | API key for Hindsight itself | **Not set.** |

---

## 7. Retrieval, entity observations, retain, workers

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_API_GRAPH_RETRIEVER`, `RECALL_MAX_CONCURRENT`, `RERANKER_MAX_CANDIDATES` | Retrieval | **Hindsight defaults.** |
| `HINDSIGHT_API_OBSERVATION_*` | Entity observations | **Hindsight defaults.** |
| `HINDSIGHT_API_RETAIN_*` | Retain pipeline | **Hindsight defaults.** |
| `HINDSIGHT_API_WORKER_*` | Internal worker | **Hindsight defaults.** |

---

## 8. Control Plane (Web UI)

We run the Control Plane as a **second ECS Fargate service** (Node container running `npx @vectorize-io/hindsight-control-plane`). It has its **own ALB**; the API has a separate ALB.

| Variable | Description | How we supply it |
|----------|-------------|------------------|
| `HINDSIGHT_CP_DATAPLANE_API_URL` | Hindsight API URL | **CDK**: Set to the API ALB URL (`http://<api-alb-dns>`). |
| `PORT` | Control Plane port | **`cdk/config.py`** → `HINDSIGHT_CONTROL_PLANE_PORT` (9999). |
| `HOSTNAME` | Bind address | **CDK hardcoded** `0.0.0.0` so the ALB can reach it. |

---

## 9. Flow overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  cdk/config.py                                                          │
│  LLM_PROVIDER, LLM_MODEL, HINDSIGHT_API_PORT, ...                       │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  CDK (hindsight_stack.py)                                               │
│  • Creates Secrets Manager secrets (RDS, LLM key, DB URL)               │
│  • ECS task: env from config; secrets from Secrets Manager              │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  GitHub Actions workflow                                                │
│  • Reads GitHub Secret: LLM_API_KEY                                     │
│  • After deploy: gets LlmSecretArn from stack outputs → put-secret-value│
│  • Force ECS redeploy so new tasks pick up LLM key                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Quick reference

| Config | Source | Where to change |
|--------|--------|------------------|
| **DB URL** | Secrets Manager (workflow writes it) | Automatically set from RDS; fix workflow if wrong. |
| **LLM provider** | `cdk/config.py` | Edit `LLM_PROVIDER`, redeploy. |
| **LLM model** | `cdk/config.py` | Edit `LLM_MODEL`, redeploy. |
| **LLM API key** | GitHub Secret `LLM_API_KEY` → Secrets Manager (via outputs after deploy) | Update GitHub Secret; workflow syncs on next deploy. |
| **Port** | `cdk/config.py` | Edit `HINDSIGHT_API_PORT`, redeploy. |
| **Control Plane URL** | CDK output | Second ALB; see `ControlPlaneUrl` after deploy. |
| **Everything else** | Hindsight defaults | Override in CDK task definition if needed. |
