"""
OpenAI adapter — implements LLMClient using the OpenAI Chat Completions API.

Uses gpt-4o-mini by default (cheapest capable model).
Supports the same run_conversation() path as the Gemini adapter so the
agent's tool-call loop works without changes.
"""
from __future__ import annotations
import os
import uuid
from typing import Any, Callable

from openai import OpenAI

from interfaces.llm import LLMClient, LLMResponse, ToolCall, ToolSpec


class OpenAIClient(LLMClient):
    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def _to_openai_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": t.parameters,
                        "required": t.required,
                    },
                },
            }
            for t in tools
        ]

    def _to_openai_messages(
        self, system_prompt: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            role = m["role"]
            if role in ("user", "assistant"):
                out.append({"role": role, "content": m["content"]})
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("call_id", m["name"]),
                    "content": str(m["content"]),
                })
        return out

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_messages(system_prompt, messages),
        }
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)
            kwargs["tool_choice"] = "auto"

        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        text_out = msg.content or ""
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            import json
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments),
                        call_id=tc.id,
                    )
                )
        return LLMResponse(text=text_out, tool_calls=tool_calls)

    def run_conversation(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, Any]],
        tools: list[ToolSpec],
        tool_executor: Callable[[str, dict[str, Any]], str],
        max_rounds: int = 5,
    ) -> str:
        import json

        messages = self._to_openai_messages(system_prompt, history)
        messages.append({"role": "user", "content": user_message})
        openai_tools = self._to_openai_tools(tools) if tools else []

        for _ in range(max_rounds):
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            if openai_tools:
                kwargs["tools"] = openai_tools
                kwargs["tool_choice"] = "auto"

            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message

            if not msg.tool_calls:
                return msg.content or "[No answer produced.]"

            # Append assistant message with tool_calls
            messages.append(msg.model_dump(exclude_unset=False))

            # Execute each tool and append results
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = tool_executor(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        return "[Max tool rounds reached without final answer.]"
