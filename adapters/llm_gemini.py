"""
Gemini adapter — implements LLMClient using Google's Gemini API (free tier).

Uses Gemini's native chat session with function calling, which correctly
handles the tool-call -> tool-result -> narration cycle. This avoids the
strict turn-ordering issues that break a hand-rolled loop.
"""
from __future__ import annotations
import os
from typing import Any, Callable

import google.generativeai as genai

from interfaces.llm import LLMClient, LLMResponse, ToolCall, ToolSpec


class GeminiClient(LLMClient):
    def __init__(self, model: str = "gemini-3.1-flash-lite") -> None:
        api_key = os.environ["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        self.model_name = model

    def _to_gemini_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        declarations = []
        for t in tools:
            decl: dict[str, Any] = {"name": t.name, "description": t.description}
            if t.parameters:
                decl["parameters"] = {
                    "type": "object",
                    "properties": t.parameters,
                    "required": t.required,
                }
            declarations.append(decl)
        return [{"function_declarations": declarations}]

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        """Single-shot (kept for interface compatibility)."""
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
            tools=self._to_gemini_tools(tools) if tools else None,
        )
        contents = []
        for m in messages:
            if m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
        response = model.generate_content(contents)

        text_out = ""
        tool_calls: list[ToolCall] = []
        for cand in response.candidates:
            for part in cand.content.parts:
                fc = getattr(part, "function_call", None)
                if fc and fc.name:
                    tool_calls.append(
                        ToolCall(name=fc.name, arguments=dict(fc.args) if fc.args else {}, call_id=fc.name)
                    )
                elif getattr(part, "text", None):
                    text_out += part.text
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
        """Run a full turn with automatic tool calling via Gemini chat session."""
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
            tools=self._to_gemini_tools(tools) if tools else None,
        )

        gemini_history = []
        for m in history:
            if m["role"] == "user":
                gemini_history.append({"role": "user", "parts": [m["content"]]})
            elif m["role"] == "assistant":
                gemini_history.append({"role": "model", "parts": [m["content"]]})

        chat = model.start_chat(history=gemini_history)
        response = chat.send_message(user_message)

        for _ in range(max_rounds):
            fcs = []
            for part in response.candidates[0].content.parts:
                fc = getattr(part, "function_call", None)
                if fc and fc.name:
                    fcs.append(fc)

            if not fcs:
                break

            tool_responses = []
            for fc in fcs:
                args = dict(fc.args) if fc.args else {}
                result_text = tool_executor(fc.name, args)
                tool_responses.append(
                    {"function_response": {"name": fc.name, "response": {"result": result_text}}}
                )
            response = chat.send_message(tool_responses)

        final = ""
        for part in response.candidates[0].content.parts:
            if getattr(part, "text", None):
                final += part.text
        return final or "[No answer produced.]"
