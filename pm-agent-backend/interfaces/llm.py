"""
LLM interface (a "port" in hexagonal architecture).

The agent loop talks ONLY to this interface, never to Gemini or Bedrock directly.
Each provider (Gemini locally, Bedrock on AWS) implements this contract in adapters/.
This is what makes the LLM a one-line swap between local and AWS.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    """A tool the LLM is allowed to call, in a provider-neutral shape."""
    name: str
    description: str
    # JSON-schema-style parameter definition, e.g.
    # {"project_key": {"type": "string", "description": "Jira project key"}}
    parameters: dict[str, Any]
    required: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    """The LLM's request to call a tool."""
    name: str
    arguments: dict[str, Any]
    call_id: str = ""  # some providers need an id to match results back


@dataclass
class LLMResponse:
    """
    A provider-neutral response.
    Either the model produced text, or it asked to call tools, or both.
    """
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient(ABC):
    """Contract that Gemini and Bedrock adapters both implement."""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        """
        Send the conversation to the model and get back either text or tool calls.

        messages: list of {"role": "user"|"assistant"|"tool", "content": ...}
                  in a neutral shape; the adapter converts to the provider format.
        """
        raise NotImplementedError
