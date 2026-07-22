"""
Config — the single place that decides which adapters to load.

This is what makes local vs AWS a one-switch change:
  - LLM_PROVIDER=gemini  -> GeminiClient   (local dev)
  - LLM_PROVIDER=bedrock -> BedrockClient  (aws)
  - APP_ENV=local        -> JsonSessionStore
  - APP_ENV=aws          -> DynamoSessionStore

Nothing else in the codebase imports a concrete adapter — everything goes
through get_llm() and get_session_store(), so swapping is trivial.
"""
from __future__ import annotations
import os

from dotenv import load_dotenv

from interfaces.llm import LLMClient
from interfaces.storage import SessionStore

# Load .env into environment (no-op on AWS where vars come from Lambda config)
load_dotenv()

APP_ENV = os.getenv("APP_ENV", "local")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")


def get_llm() -> LLMClient:
    """Return the active LLM client based on LLM_PROVIDER."""
    if LLM_PROVIDER == "gemini":
        from adapters.llm_gemini import GeminiClient
        return GeminiClient()
    if LLM_PROVIDER == "bedrock":
        from adapters.llm_bedrock import BedrockClient
        return BedrockClient()
    if LLM_PROVIDER == "openai":
        from adapters.llm_openai import OpenAIClient
        return OpenAIClient(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")


def get_mcp_client():
    """Return the MCP client. InlineMCPClient is used in all environments —
    it calls Jira/Slack functions directly with no subprocess dependency."""
    from adapters.mcp_inline import InlineMCPClient
    return InlineMCPClient()


def get_session_store() -> SessionStore:
    """Return the active session store based on APP_ENV."""
    if APP_ENV == "local":
        from adapters.storage_json import JsonSessionStore
        return JsonSessionStore()
    if APP_ENV == "aws":
        from adapters.storage_dynamo import DynamoSessionStore
        return DynamoSessionStore()
    raise ValueError(f"Unknown APP_ENV: {APP_ENV}")
