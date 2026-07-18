# PM Delivery Agent — Backend

An AI agent that reads live Jira data and answers PM questions (status, risks),
with Slack Human-in-the-Loop approvals. Built local-first, deploys to AWS with
a one-line config switch.

## Architecture at a glance

```
CLI / FastAPI  ->  Agent Core (the loop)  ->  LLM (Gemini local / Bedrock AWS)
                          |
                          +-->  MCP Client  ->  MCP Server (separate process)
                                                     |
                                                     +-->  Jira REST
                                                     +-->  Slack webhook
```

- **Agent Core** (`agent/core.py`) — domain-agnostic reasoning loop.
- **MCP Server** (`mcp_servers/pm_server/`) — exposes tools over the Model Context
  Protocol. The agent knows nothing about Jira/Slack; it only speaks MCP.
- **Adapters** (`adapters/`) — swappable LLM and storage. One env var picks which.

## Cross-platform: Windows AND Mac

This project runs identically on both. The **only** differences are a couple of
setup commands (creating/activating the virtual environment). The application
code, run commands, and everything else are the same. Paths use Python's
`pathlib`, and the MCP subprocess uses the same interpreter, so there is no
platform-specific code.

---

## Prerequisites

- Python 3.10+
- A Gemini API key (free): https://aistudio.google.com -> Get API key
- (Optional for now) Jira account + token, Slack webhook

---

## Setup

### 1. Create and activate a virtual environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
```

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

> This is the ONLY step that differs by OS. Everything below is identical.

### 2. Install dependencies (same on all platforms)
```
pip install -r requirements.txt
```

### 3. Create your .env
Copy the template and fill in your Gemini key:
```
# Windows (PowerShell):  copy .env.example .env
# Mac / Linux:           cp .env.example .env
```
Then edit `.env` and set:
```
GEMINI_API_KEY=your-real-key
```
Leave Jira/Slack values as-is for now — the agent still runs; those tools just
return "not configured" until you add real credentials.

---

## Run

### Option A — Talk to the agent in your terminal (fastest test)
```
python cli.py
```
Try: `what tools do you have?` or `what's the status of project EPM?`
(Without Jira configured, it will tell you there's no Jira data yet — that's
expected. The loop, LLM, and MCP wiring all work.)

### Option B — Run the API for the frontend
```
uvicorn main:app --reload --port 8000
```
Then:
- Health check: http://localhost:8000/health
- Chat endpoint: POST http://localhost:8000/chat
  body: `{ "session_id": "test1", "message": "status of EPM?" }`

---

## Switching to AWS later (preview)

Change two lines in `.env`:
```
APP_ENV=aws
LLM_PROVIDER=bedrock
```
That swaps JSON storage -> DynamoDB and Gemini -> Bedrock. No code changes.
(Full AWS deploy adds a Lambda handler + SAM template — covered in a later step.)

---

## Project layout

```
pm-agent-backend/
├── agent/
│   ├── core.py          # the reasoning loop (the brain)
│   └── mcp_client.py    # connects to the MCP server
├── adapters/
│   ├── llm_gemini.py    # LOCAL LLM (active)
│   ├── llm_bedrock.py   # AWS LLM (ready)
│   ├── storage_json.py  # LOCAL storage (active)
│   └── storage_dynamo.py# AWS storage (ready)
├── interfaces/          # contracts (llm, storage)
├── mcp_servers/
│   └── pm_server/
│       ├── server.py    # the MCP server
│       ├── jira_client.py
│       └── slack_client.py
├── shared/
│   └── config.py        # the one switch: picks adapters from env
├── cli.py               # terminal chat
├── main.py              # FastAPI app
├── requirements.txt
├── .env.example
└── .gitignore
```
