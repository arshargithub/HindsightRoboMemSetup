# AWS Architecture for Hindsight Memory Service

This document describes a complete AWS-based architecture for running [Hindsight](https://hindsight.vectorize.io) as a memory service for the Johnny robot.

**Summary:**
- **Hindsight infra**: **PostgreSQL + pgvector only**. No separate vector DB or document DB; everything (relational + vectors) lives in Postgres.
- **Deployment**: **ECS Fargate** only (no VMs). Dev and prod **both on AWS**; nothing runs locally.
- **Reflect**: **EventBridge Scheduler + Lambda** — batch, scheduled Reflect jobs; not part of the robot interaction path.

All details are based on official sources:
- [Hindsight Installation](https://hindsight.vectorize.io/developer/installation)
- [Hindsight Configuration](https://hindsight.vectorize.io/developer/configuration)
- [Hindsight Services](https://hindsight.vectorize.io/developer/services)
- [Hindsight Operations](https://hindsight.vectorize.io/developer/api/operations)
- [GitHub: vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)

---

## 1. What Infra Does Hindsight Require?

### Single database: PostgreSQL + pgvector

Hindsight uses **one database**: **PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension**.

- **Relational data**: facts, entities, memory banks, operations, etc. live in normal Postgres tables.
- **Vector search**: pgvector stores embeddings and powers semantic similarity search (TEMPR's vector strategy).
- There is **no** separate vector DB (e.g. Pinecone, Weaviate) or document DB (e.g. MongoDB). Everything is in Postgres.

From the [Installation](https://hindsight.vectorize.io/developer/installation) docs:

> Hindsight requires **PostgreSQL with the pgvector extension** for vector similarity search.  
> For production, use an external PostgreSQL with pgvector: **AWS RDS** / Cloud SQL / Azure / self‑hosted (Postgres 14+).

### LLM provider (external API)

- **Retain** (fact extraction) and **Reflect** (reasoning, opinion formation) call an LLM.
- **Recall** does **not** use an LLM; it's retrieval only.
- You configure a provider (OpenAI, Groq, Anthropic, Gemini, etc.) via env vars and API keys. No LLM infra runs inside your AWS account.

### Embeddings and reranker

- **Embeddings**: Used for vector search. Can be **local** (SentenceTransformers in the API container) or **external** (OpenAI, Cohere, TEI, LiteLLM).
- **Reranker**: Used in Recall. Can be **local** (cross‑encoder in‑process) or **external** (Cohere, TEI, etc.).

For ECS/Fargate, **local** embeddings/reranker are fine for moderate load; the Hindsight image runs them in-process. For larger scale, you'd typically switch to cloud embeddings (e.g. OpenAI) via config.

### Summary

| Component        | What Hindsight uses              | Runs in AWS?        |
|-----------------|-----------------------------------|---------------------|
| **Database**    | PostgreSQL + pgvector only        | ✅ Yes (RDS/Aurora) |
| **Vector store**| pgvector (inside Postgres)        | ✅ Same as above    |
| **LLM**         | External API (OpenAI, Groq, etc.) | ❌ No               |
| **Embeddings**  | Local or external (configurable)  | Optional            |
| **Reranker**    | Local or external (configurable)  | Optional            |

---

## 2. High‑Level Architecture

- **Dev** and **Prod**: two separate environments on AWS. No Hindsight (or robot backend) running locally.
- **Compute**: ECS Fargate only. No EC2 or other VMs.
- **Reflect**: Run on a **schedule** (batch intervals), not per interaction. Use **EventBridge Scheduler** plus **Lambda** to HTTP‑call Hindsight's Reflect API.

---

## 3. Per‑Environment Architecture (Dev or Prod)

Each environment (dev / prod) contains the same building blocks.

### 3.1 Components

| Component | Purpose |
|-----------|---------|
| **ALB** | HTTPS termination, routes `/` to Hindsight API (and optionally to Control Plane). Robot and Lambda call `https://<alb-dns>/...` |
| **ECS Fargate – Hindsight API** | Single service running `hindsight-api`. Connects to RDS, uses LLM via env (Secrets Manager). Exposes port 8888. |
| **ECS Fargate – Control Plane** | Optional. Runs Control Plane UI, points at Hindsight API URL. For debugging / inspecting banks. |
| **RDS PostgreSQL** | Postgres 14+ (or 15/16) with pgvector. One instance (or Aurora cluster) per env. Hindsight stores all data here. |
| **Secrets Manager** | RDS user/password, LLM API key(s), optional `HINDSIGHT_API_TENANT_API_KEY`. |
| **EventBridge Scheduler** | Triggers the Reflect job on a **schedule** (e.g. every 15 min or during "low activity" windows). |
| **Lambda – Reflect job** | Invoked by EventBridge. Calls Hindsight Reflect API over HTTP (e.g. `POST /v1/default/banks/<bank>/reflect` or equivalent per [Reflect API](https://hindsight.vectorize.io/developer/api/reflect)). No containers; serverless. |

### 3.2 Hindsight Services on ECS

From [Services](https://hindsight.vectorize.io/developer/services):

- **API**: `hindsight-api`. Stateless; scale via Fargate tasks. All state in Postgres.
- **Worker**: Optional. Same image, different entrypoint (`hindsight-worker`). For heavy async workloads. For a single robot, the **internal worker** (default) is usually enough; no separate Worker service.
- **Control Plane**: Optional Web UI. Separate container (or npx), points at API URL.

So **minimum** ECS footprint: one **Hindsight API** service. Add Control Plane and/or Worker only if needed.

---

## 4. Networking (VPC)

- **Public subnets**: ALB.
- **Private subnets**: ECS Fargate tasks (API, Control Plane), RDS.
- **NAT Gateway** (or NAT instance) in public subnet so Fargate can reach LLM APIs (and any external embeddings) and Lambda can reach Hindsight if the API is internal.
- **Security groups**:
  - ALB: Allow 443 from robot + from Lambda (if Reflect hits ALB).
  - ECS: Allow traffic from ALB on 8888 (and Control Plane port if different).
  - RDS: Allow 5432 only from ECS tasks (and optionally from Lambda if you ever add direct DB access; not needed for Reflect).
  - Lambda: If in VPC, allow outbound to ALB (and NAT for internet if needed).

---

## 5. RDS PostgreSQL + pgvector

- **Engine**: PostgreSQL 15 or 16 (or 14 minimum). Use [RDS PostgreSQL pgvector](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html) support.
- **Extension**: `CREATE EXTENSION vector;` in the Hindsight DB.
- **Single instance** per env is enough to start; switch to Aurora if you need HA/multi‑AZ.
- **Connection string** via Secrets Manager, passed to Hindsight as `HINDSIGHT_API_DATABASE_URL`.

---

## 6. ECS Task Definition (Hindsight API)

- **Image**: `ghcr.io/vectorize-io/hindsight:latest` (or fixed tag).
- **Command**: `hindsight-api` (default).
- **Env** (from Secrets Manager + SSM):
  - `HINDSIGHT_API_DATABASE_URL`
  - `HINDSIGHT_API_LLM_PROVIDER`, `HINDSIGHT_API_LLM_API_KEY`, `HINDSIGHT_API_LLM_MODEL`
  - Optional: `HINDSIGHT_API_EMBEDDINGS_*`, `HINDSIGHT_API_RERANKER_*`, `HINDSIGHT_API_TENANT_*`
- **Port**: 8888.
- **Logging**: CloudWatch Logs.
- **Tasks**: 1 to start; increase for availability or load.

---

## 7. Reflect Scheduling (EventBridge + Lambda)

Reflect is **not** part of the request path. It runs on a **schedule** as a batch job.

### 7.1 Option A: EventBridge + Lambda (recommended)

- **EventBridge Scheduler** (or Rules): rate or cron (e.g. `rate(15 minutes)` or `cron(0 * * * ? *)` for hourly).
- **Target**: **Lambda** function.
- **Lambda**:
  - Reads Hindsight API URL and bank ID from env (or SSM).
  - Uses stored API key if you enable Hindsight auth.
  - Sends HTTP request to Hindsight **Reflect** endpoint (see [Reflect API](https://hindsight.vectorize.io/developer/api/reflect)), e.g. with a generic prompt like "Summarise recent learnings and update opinions" or per‑bank queries you define.
  - Logs success/failure to CloudWatch.

**Pros**: No extra containers, minimal ops, easy to change schedule per env (e.g. dev every hour, prod every 15 min).

### 7.2 Option B: EventBridge + ECS Run Task

- **EventBridge** triggers **ECS Run Task** (Fargate) instead of Lambda.
- **Task**: Small "reflect job" container that runs `hindsight-client` (or `curl`), calls Reflect, then exits.

**Pros**: All logic in containers. **Cons**: More to build and maintain (image, task def, IAM).

### 7.3 Recommendation

Use **EventBridge + Lambda** for Reflect. It matches "batch, scheduled, non‑interactive" and keeps the rest of the stack container‑focused (ECS for Hindsight only).

---

## 8. Dev vs Prod

| Aspect | Dev | Prod |
|--------|-----|------|
| **VPC** | Separate VPC (or dev subnet) | Separate VPC (or prod subnet) |
| **RDS** | Single small instance (e.g. db.t3.micro) | Instance or Aurora, sized for load |
| **ECS** | 1 task for API; optional Control Plane | 1+ tasks, multi‑AZ if desired |
| **ALB** | Dev ALB (or path‑based) | Prod ALB |
| **Secrets** | Dev secrets (e.g. dev LLM key, dev DB) | Prod secrets |
| **Reflect schedule** | Less frequent (e.g. hourly) | Per your design (e.g. every 15 min) |
| **DNS / URL** | e.g. `hindsight-dev.<your-domain>` | e.g. `hindsight.<your-domain>` |

Use separate **ECS clusters** (e.g. `hindsight-dev`, `hindsight-prod`) or separate **services** in one cluster; keep dev and prod clearly isolated.

---

## 9. Minimal Resource Checklist

- [ ] **VPC** (dev, prod) with public/private subnets, NAT.
- [ ] **RDS** Postgres 14+ with pgvector, one per env.
- [ ] **Secrets Manager**: RDS creds, LLM keys, optional Hindsight API key.
- [ ] **ECS cluster** (per env).
- [ ] **Fargate task definition** for Hindsight API; optional one for Control Plane.
- [ ] **ECS service(s)** behind an **ALB** (HTTPS, ACM cert).
- [ ] **Lambda** for Reflect job; **EventBridge** schedule.
- [ ] **IAM** roles for ECS tasks, Lambda, minimal least‑privilege.

---

## 10. References

- [Hindsight – Installation](https://hindsight.vectorize.io/developer/installation)
- [Hindsight – Configuration](https://hindsight.vectorize.io/developer/configuration)
- [Hindsight – Services](https://hindsight.vectorize.io/developer/services)
- [Hindsight – Reflect API](https://hindsight.vectorize.io/developer/api/reflect)
- [Hindsight – Operations](https://hindsight.vectorize.io/developer/api/operations)
- [Hindsight – Retain API](https://hindsight.vectorize.io/developer/api/retain)
- [Hindsight – Recall API](https://hindsight.vectorize.io/developer/api/recall)
- [GitHub: vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)
- [AWS RDS PostgreSQL pgvector](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html)
