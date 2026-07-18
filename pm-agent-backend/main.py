"""
FastAPI app — the local API entry point for the frontend.

Exposes:
  GET  /health        -> quick check
  POST /chat          -> { "session_id": "...", "message": "..." } -> { "reply": "..." }

The agent logic lives in agent/core.py; this file is a thin shell around it
(the same core will be wrapped by a Lambda handler on AWS — logic unchanged).

Run (Windows & Mac, from project root, venv active):
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.core import Agent
from agent.mcp_client import MCPClient
from shared.config import get_llm, get_session_store

# Shared singletons for the app's lifetime
_llm = get_llm()
_store = get_session_store()
_mcp = MCPClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect to the MCP server once when the API starts
    await _mcp.connect()
    yield
    # Clean up on shutdown
    await _mcp.close()


app = FastAPI(title="PM Delivery Agent", lifespan=lifespan)

# CORS so the React frontend (different origin) can call this API.
# For local dev we allow localhost; tighten to your CloudFront domain on AWS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
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
