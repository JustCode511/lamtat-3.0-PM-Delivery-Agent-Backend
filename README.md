# AI Delivery Intelligence — Backend

FastAPI backend for a multi-agent delivery platform. Three agent modules share one
codebase, one auth layer, and one storage abstraction:

- **PM Delivery Agent** — reads live Jira data and answers PM questions (status,
  risks, milestones, team workload), generates leadership reports & PowerPoint decks,
  and posts to Slack via a Human-in-the-Loop "Approve & Send" flow.
- **Talent Management Agent** — skills, capacity, and people-to-work matching (`talent/`).
- **Cloud FinOps Agent** — live AWS cost visibility, anomaly & rightsizing insights
  via Cost Explorer (`finops/`).

Built **local-first** (JSON storage + Gemini) and deployed to **AWS** (DynamoDB +
Bedrock + Lambda) by flipping two env vars — no code changes.

---

## Architecture at a glance

```
FastAPI (main.py)  ──►  Agent Core  ──►  LangGraph supervisor graph
   │  Mangum on Lambda        │              START → supervisor → node → aggregator → END
   │                          │              nodes: query · create_issue · generate_ppt
   │                          │                     · send_slack · out_of_scope · default
   │                          ▼
   │                   Inline MCP client (adapters/mcp_inline.py)
   │                          ├─► Jira REST      (jira_client)
   │                          └─► Slack          (slack_client)
   ▼
LLM adapter  ──►  Gemini (local)  |  Bedrock Claude Sonnet 4.6 (AWS)
```

- **Ports & adapters** — every external dependency sits behind an interface
  (`interfaces/`) with a local (JSON) and an AWS (DynamoDB) adapter (`adapters/`).
  `shared/config.py` is the *single switch*: it reads `APP_ENV` / `LLM_PROVIDER`
  and picks the concrete adapters. Nothing else imports a concrete adapter.
- **Supervisor graph** (`agent/graph.py`, `supervisor.py`) — an LLM classifies the
  request into an intent, then routes to a specialized node. **Critical routing is
  deterministic** (rule-based fast-paths) so the UI behaves the same every time —
  see below.
- **Inline MCP** — tools are called in-process (`adapters/mcp_inline.py`); no separate
  MCP subprocess to manage on Lambda.

---

## Key capabilities

**Chat & reports**
- **Async chat (poll-for-result)** — `POST /pm/chat/async` returns instantly and runs
  the agent in a **separate background Lambda invocation**; the client polls
  `GET /pm/chat/result/{job_id}`. This side-steps API Gateway's hard 30-second cap, so
  long reports (all-projects, "risks with resolutions") never 504. Results are saved to
  the `pm_jobs` table.
- **Leadership reports** with a Human-in-the-Loop **Approve & Send** step (the report is
  generated for review; a human approves before it posts to the leadership Slack channel).
- **PowerPoint export** — `GET /export/ppt[?project_key=KEY]` streams a `.pptx` generated
  on the fly (no temp files). Works per-project and for the whole portfolio.
- **Conversation history** — every turn is archived per user (`/pm/conversations`),
  with **ownership-scoped delete** (`DELETE /pm/conversations/{id}`), plus rolling
  **long-term memory** (session summaries + cross-session user facts).

**Reliability — deterministic routing** (no flaky LLM classification for UI-critical paths):
- PPT keywords (`ppt`, `powerpoint`, `slides`, `deck`, `presentation`) → always `generate_ppt`,
  and the download link is guaranteed in the reply.
- "report/summary … for **leadership/stakeholders**" → always the read/analyse path with
  the Approve & Send buttons (typo-tolerant — keyed on the *audience*, so "summay" still works).
- "**all projects** / every project / portfolio" → always the whole-portfolio scope (`__ALL__`).

**Performance**
- Parallel tool execution + a "batch your tool calls" prompt cut a report from 3 → 2
  sequential LLM round-trips.
- `maxTokens = 8192` on the answer step so detailed multi-project reports aren't truncated.

**Security**
- JWT auth (PBKDF2-hashed passwords) with server-side sign-out revocation (denylist).
- On AWS, secrets load from **SSM Parameter Store** (SecureString) at cold start; the
  Lambda runs under a least-privilege IAM role.

---

## AWS deployment (Terraform in the separate `pm-agent-infra` repo)

```
CloudFront ──► /          ──► private S3 (SPA)
           └─► /api/*      ──► API Gateway (HTTP) ──► Lambda (Mangum + FastAPI)
                                                        ├─► Bedrock (Claude Sonnet 4.6)
                                                        ├─► DynamoDB × 6
                                                        ├─► SSM (secrets)  · self-invoke (jobs)
                                                        └─► Cost Explorer (FinOps, read-only)
```

DynamoDB tables: `pm_sessions`, `pm_users`, `pm_revoked_tokens`, `pm_conversations`,
`pm_memory`, `pm_jobs` (all on-demand → ~$0 idle). An EventBridge rule pings `/health`
every 5 min to keep the function warm (free). Bedrock model: `global.anthropic.claude-sonnet-4-6`.

---

## Prerequisites
- Python 3.10+
- A Gemini API key (free): https://aistudio.google.com → Get API key
- (Optional) Jira account + token, Slack bot token/webhook

## Setup

**1. Virtual environment** (the only OS-specific step):
```bash
# Mac / Linux
python3 -m venv .venv && source .venv/bin/activate
# Windows (PowerShell)
python -m venv .venv; .\.venv\Scripts\Activate.ps1
```

**2. Dependencies:**
```bash
pip install -r requirements.txt
```

**3. Create `.env`** (copy `.env.example`) and set at least:
```
GEMINI_API_KEY=your-real-key
# Jira/Slack optional — those tools return "not configured" until set
```

**4. Seed a login user** (`data/users.json` is gitignored):
```bash
python scripts/create_user.py            # admin / admin123
python scripts/create_user.py you pass   # custom
```

## Run
```bash
python cli.py                            # terminal chat (fastest smoke test)
uvicorn main:app --reload --port 8000    # API for the frontend
```
- Health: http://localhost:8000/health
- Chat (async): `POST /pm/chat/async` `{ "session_id": "s1", "message": "status of AABGFY26?" }`
  then poll `GET /pm/chat/result/{job_id}`

## Local → AWS switch
Flip two env vars (Lambda sets these automatically):
```
APP_ENV=aws          # JSON storage → DynamoDB
LLM_PROVIDER=bedrock # Gemini → Bedrock Claude
```
On Lambda the entrypoint is `main.handler` (Mangum). Background jobs arrive as a
`{"__job__": …}` event and run the agent worker; everything else is normal HTTP.

---

## Project layout
```
pm-agent-backend/
├── main.py                 # FastAPI app + Mangum handler + background job worker
├── cli.py                  # terminal chat
├── agent/
│   ├── core.py             # Agent (graph.ainvoke wrapper)
│   ├── graph.py            # LangGraph wiring: supervisor → node → aggregator
│   ├── supervisor.py       # intent classifier + deterministic routing overrides
│   ├── nodes.py            # query / create_issue / generate_ppt / slack / clarify …
│   ├── pm_query_agent.py   # tool-calling read/analysis agent (parallel tools)
│   ├── aggregator.py, state.py
├── adapters/               # concrete implementations (local + AWS per port)
│   ├── llm_{gemini,bedrock,openai}.py
│   ├── mcp_inline.py       # in-process Jira/Slack tools
│   ├── ppt_generator.py    # .pptx builder
│   ├── {storage,user,denylist,conversation,memory,job}_{json,dynamo}.py
├── interfaces/             # ports: llm, storage, user_store, token_denylist,
│                           #        conversation_store, memory_store, job_store
├── shared/
│   ├── config.py           # THE switch — picks adapters from env
│   ├── auth.py             # JWT + PBKDF2
│   ├── aws_secrets.py      # SSM loader (cold start)
│   ├── long_term_memory.py, observability.py
├── talent/                 # Talent Management agent + routes
├── finops/                 # Cloud FinOps agent (Cost Explorer, read-only)
├── scripts/create_user.py
├── requirements.txt, .env.example, .gitignore
```
