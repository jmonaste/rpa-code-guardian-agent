"""Single gateway for every LLM call: structured output and tool loops.

Local models behind OpenAI-compatible endpoints are less reliable than hosted
frontier models, so all robustness lives here in one place:

* ``structured()`` asks for a Pydantic schema via tool calling, retries once
  with the validation error, then falls back to parsing JSON out of a plain
  completion.
* ``tool_loop()`` runs a bounded think-act-observe loop (the obsidian-agent
  pattern) and returns the final answer plus the tools that were actually used.

Tests inject a fake subclass, so no other module talks to the model directly.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ValidationError

from .config import Settings

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class GuardianLLMError(RuntimeError):
    """Raised when the model could not produce usable output after retries."""


class GuardianLLM:
    """Two model slots (worker/lead) over one OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._models: dict[str, object] = {}

    def _chat_model(self, role: str):
        if role not in self._models:
            from langchain_openai import ChatOpenAI

            model_name = (
                self.settings.resolved_worker_model()
                if role == "worker"
                else self.settings.resolved_lead_model()
            )
            self._models[role] = ChatOpenAI(
                model=model_name,
                base_url=self.settings.openai_base_url,
                api_key=self.settings.openai_api_key,
                temperature=self.settings.temperature,
            )
        return self._models[role]

    # ------------------------------------------------------------------ #

    def structured(self, schema: type[T], system: str, user: str, role: str = "worker") -> T:
        """Return an instance of ``schema`` produced by the model."""
        llm = self._chat_model(role)
        try:
            runner = llm.with_structured_output(schema, method="function_calling")
            result = runner.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            if isinstance(result, schema):
                return result
        except Exception as first_error:  # noqa: BLE001 - endpoint/validation quirks
            retry_note = (
                f"\n\nYour previous attempt failed with: {first_error}."
                " Respond again, satisfying the schema exactly."
            )
            try:
                runner = llm.with_structured_output(schema, method="function_calling")
                result = runner.invoke(
                    [SystemMessage(content=system), HumanMessage(content=user + retry_note)]
                )
                if isinstance(result, schema):
                    return result
            except Exception:  # noqa: BLE001
                pass
        return self._structured_via_json(schema, system, user, role)

    def _structured_via_json(self, schema: type[T], system: str, user: str, role: str) -> T:
        """Fallback: plain completion asked to emit JSON matching the schema."""
        llm = self._chat_model(role)
        prompt = (
            f"{user}\n\nRespond with ONLY a JSON object matching this schema"
            f" (no prose, no code fences):\n{json.dumps(schema.model_json_schema(), indent=1)}"
        )
        reply = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise GuardianLLMError(f"model returned no JSON for {schema.__name__}")
        try:
            return schema.model_validate_json(match.group(0))
        except ValidationError as exc:
            raise GuardianLLMError(f"invalid {schema.__name__} JSON: {exc}") from exc

    # ------------------------------------------------------------------ #

    def tool_loop(
        self,
        system: str,
        user: str,
        tools: list,
        role: str = "lead",
        max_iterations: int | None = None,
    ) -> tuple[str, list[BaseMessage]]:
        """Bounded think-act-observe loop; returns (final_text, transcript)."""
        limit = max_iterations or self.settings.agent_max_iterations
        llm = self._chat_model(role).bind_tools(tools)
        by_name = {t.name: t for t in tools}
        messages: list[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]

        for _ in range(limit):
            reply = llm.invoke(messages)
            messages.append(reply)
            calls = getattr(reply, "tool_calls", None) or []
            if not calls:
                text = reply.content if isinstance(reply.content, str) else str(reply.content)
                return text, messages
            for call in calls:
                tool = by_name.get(call["name"])
                if tool is None:
                    output = f"ERROR: unknown tool {call['name']!r}"
                else:
                    try:
                        output = str(tool.invoke(call.get("args", {})))
                    except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                        output = f"ERROR: {exc}"
                messages.append(ToolMessage(content=output, tool_call_id=call["id"], name=call["name"]))

        # Out of budget: ask for a final answer without tools.
        messages.append(HumanMessage(content="Tool budget exhausted. Answer now with what you have."))
        reply = self._chat_model(role).invoke(messages)
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        messages.append(AIMessage(content=text))
        return text, messages
