"""
Gemini adapter — implements LLMClient using Google's Gemini API (free tier).

This is the ACTIVE adapter for local development.
It translates between our neutral interface and Gemini's function-calling format,
so the agent loop never sees anything Gemini-specific.
"""
from __future__ import annotations
import os
from typing import Any

import google.generativeai as genai

from interfaces.llm import LLMClient, LLMResponse, ToolCall, ToolSpec


class GeminiClient(LLMClient):
    def __init__(self, model: str = "gemini-2.5-flash") -> None:
        api_key = os.environ["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        self.model_name = model

    # ---- convert our neutral tools -> Gemini's tool format ----
    def _to_gemini_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        function_declarations = []
        for t in tools:
            function_declarations.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": t.parameters,
                        "required": t.required,
                    },
                }
            )
        return [{"function_declarations": function_declarations}]

    # ---- convert our neutral messages -> Gemini's content format ----
    def _to_gemini_contents(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
            elif role == "tool":
                # tool result fed back to the model
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": m["name"],
                                    "response": {"result": m["content"]},
                                }
                            }
                        ],
                    }
                )
        return contents

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
            tools=self._to_gemini_tools(tools) if tools else None,
        )

        response = model.generate_content(self._to_gemini_contents(messages))

        # Parse Gemini's response into our neutral shape
        text_out = ""
        tool_calls: list[ToolCall] = []

        for candidate in response.candidates:
            for part in candidate.content.parts:
                # a function (tool) call
                fc = getattr(part, "function_call", None)
                if fc and fc.name:
                    tool_calls.append(
                        ToolCall(
                            name=fc.name,
                            arguments=dict(fc.args) if fc.args else {},
                            call_id=fc.name,
                        )
                    )
                # plain text
                elif getattr(part, "text", None):
                    text_out += part.text

        return LLMResponse(text=text_out, tool_calls=tool_calls)
