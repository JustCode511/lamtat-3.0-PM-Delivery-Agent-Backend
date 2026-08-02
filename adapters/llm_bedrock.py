"""
Bedrock adapter — implements LLMClient using AWS Bedrock (Claude Haiku).

This is the AWS adapter. It is NOT used locally (APP_ENV=local uses Gemini).
It's written now so the local->AWS swap is a one-line config change later.
On Lambda, credentials come from the IAM role automatically (no key needed).
"""
from __future__ import annotations
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3

from interfaces.llm import LLMClient, LLMResponse, ToolCall, ToolSpec


class BedrockClient(LLMClient):
    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
    ) -> None:
        # Deployment controls both via env:
        #   BEDROCK_MODEL_ID -> e.g. an APAC inference profile "apac.anthropic.claude-sonnet-..."
        #   region=None      -> boto3 uses AWS_REGION (set automatically on Lambda)
        self.model_id = model_id or os.getenv(
            "BEDROCK_MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0"
        )
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def _to_bedrock_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": t.parameters,
                            "required": t.required,
                        }
                    },
                }
            }
            for t in tools
        ]

    def _to_bedrock_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "user":
                out.append({"role": "user", "content": [{"text": m["content"]}]})
            elif role == "assistant":
                out.append({"role": "assistant", "content": [{"text": m["content"]}]})
            elif role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": m.get("call_id", m["name"]),
                                    "content": [{"text": str(m["content"])}],
                                }
                            }
                        ],
                    }
                )
        return out

    def generate(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system_prompt}],
            "messages": self._to_bedrock_messages(messages),
            "inferenceConfig": {"maxTokens": 1024, "temperature": 0.2},
        }
        if tools:
            kwargs["toolConfig"] = {"tools": self._to_bedrock_tools(tools)}

        response = self.client.converse(**kwargs)

        text_out = ""
        tool_calls: list[ToolCall] = []
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                text_out += block["text"]
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        name=tu["name"],
                        arguments=tu["input"],
                        call_id=tu["toolUseId"],
                    )
                )

        return LLMResponse(text=text_out, tool_calls=tool_calls)

    def run_conversation(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, Any]],
        tools: list[ToolSpec],
        tool_executor,
        max_rounds: int = 5,
    ) -> str:
        bedrock_tools = self._to_bedrock_tools(tools) if tools else []

        # Build initial messages from history
        messages: list[dict[str, Any]] = self._to_bedrock_messages(
            [m for m in history if m["role"] in ("user", "assistant")]
        )
        # Add current user message
        messages.append({"role": "user", "content": [{"text": user_message}]})

        for _ in range(max_rounds):
            kwargs: dict[str, Any] = {
                "modelId": self.model_id,
                "system": [{"text": system_prompt}],
                "messages": messages,
                # 8192 so a detailed multi-/all-project report is never truncated
                # mid-sentence. The model only emits what it needs, so single-project
                # reports stay short; this is purely a ceiling for the big ones.
                "inferenceConfig": {"maxTokens": 8192, "temperature": 0.2},
            }
            if bedrock_tools:
                kwargs["toolConfig"] = {"tools": bedrock_tools}

            response = self.client.converse(**kwargs)
            output_msg = response["output"]["message"]
            messages.append(output_msg)

            tool_use_blocks = [b for b in output_msg["content"] if "toolUse" in b]
            text_blocks = [b.get("text", "") for b in output_msg["content"] if "text" in b]

            if not tool_use_blocks:
                return "".join(text_blocks) or "[No answer produced.]"

            # Execute the tool calls. When Sonnet batches several tools in one turn
            # (e.g. get_project_status + flag_risks), run them CONCURRENTLY instead of
            # one-after-another — each tool_executor call is an independent Jira round-trip,
            # so parallelism turns their latency from a sum into a max. Order is preserved
            # to keep toolUseId ↔ toolResult pairing correct.
            tus = [b["toolUse"] for b in tool_use_blocks]
            if len(tus) == 1:
                results = [tool_executor(tus[0]["name"], tus[0]["input"])]
            else:
                with ThreadPoolExecutor(max_workers=len(tus)) as pool:
                    results = list(
                        pool.map(lambda tu: tool_executor(tu["name"], tu["input"]), tus)
                    )
            tool_results = [
                {
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": str(result)}],
                    }
                }
                for tu, result in zip(tus, results)
            ]
            messages.append({"role": "user", "content": tool_results})

        return "[Max tool rounds reached without final answer.]"
