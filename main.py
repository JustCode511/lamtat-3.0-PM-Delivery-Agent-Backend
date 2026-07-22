"""
FastAPI app — entry point for both local dev and AWS Lambda.

Local:   uvicorn main:app --reload --port 8000
Lambda:  the `handler` export is the Mangum-wrapped ASGI handler;
         set handler=main.handler in Lambda function config.

Endpoints:
  GET  /health   → liveness check
  POST /chat     → { "session_id": "...", "message": "..." } → { "reply": "..." }
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.core import Agent
from shared.config import get_llm, get_mcp_client, get_session_store

_llm = get_llm()
_store = get_session_store()
_mcp = get_mcp_client()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _mcp.connect()
    yield
    await _mcp.close()


app = FastAPI(title="PM Delivery Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    agent = Agent(_llm, _store, _mcp)
    reply = await agent.chat(req.session_id, req.message)
    return ChatResponse(reply=reply)


# ---------------------------------------------------------------------------
# Lambda handler — Mangum wraps the FastAPI ASGI app.
# InlineMCPClient.connect/close are no-ops so lifespan works fine on Lambda.
# ---------------------------------------------------------------------------
try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="auto")
except ImportError:
    handler = None  # mangum not installed locally; uvicorn is used instead
