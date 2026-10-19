"""Recorded LLM answers, so evaluations re-run without a network or a key.

`RecordingLLM` wraps a real provider in record mode: each structured answer is
saved under a hash of the model, the operation, the prompts and the output
schema. In replay mode it answers from those files and never calls out; a
prompt with no recording is an error (`ReplayMissing`), not a silent gap,
because a changed prompt must be re-scored deliberately, with a budget.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from app.services.llm import LLMProvider

T = TypeVar("T", bound=BaseModel)
Mode = Literal["replay", "record"]


class ReplayMissing(LookupError):
    """No recording for this prompt and model."""


def prompt_key(*, model: str, operation: str, system: str, user: str, schema: str) -> str:
    blob = json.dumps(
        {"model": model, "operation": operation, "system": system, "user": user, "schema": schema},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class RecordingLLM(LLMProvider):
    name = "recording"

    def __init__(self, directory: Path, *, mode: Mode, model: str, inner: LLMProvider | None = None) -> None:
        if mode == "record" and inner is None:
            raise ValueError("record mode needs a real provider to record")
        self.directory = directory
        self.mode = mode
        self.model = model
        self.inner = inner
        self.calls = 0
        self.replayed = 0

    @property
    def available(self) -> bool:
        return True

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    async def parse_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        operation: str,
        max_tokens: int | None = None,
    ) -> T:
        key = prompt_key(model=self.model, operation=operation, system=system, user=user, schema=output_model.__name__)
        path = self._path(key)
        self.calls += 1
        if self.mode == "replay":
            if not path.is_file():
                raise ReplayMissing(f"no recording {key[:12]} for model {self.model}; re-score with --live")
            self.replayed += 1
            return output_model.model_validate(json.loads(path.read_text())["output"])
        assert self.inner is not None
        result = await self.inner.parse_structured(
            system=system, user=user, output_model=output_model, operation=operation, max_tokens=max_tokens
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"model": self.model, "operation": operation, "schema": output_model.__name__, "output": result.model_dump(mode="json")},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return result

    async def complete(self, *, system: str, messages: list[Any], operation: str, max_tokens: int | None = None) -> str:
        raise NotImplementedError("evaluations record structured scoring calls and tool turns only")

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[Any],
        operation: str,
        max_tokens: int | None = None,
        on_text: Any = None,
    ) -> Any:
        """Ask Metrix's tool turns, recorded and replayed like scoring calls,
        keyed by the whole transcript (tool results included) and the tools.
        Tool results carry database ids, so recordings belong to the database
        they were made on (the chat eval's seeded one)."""
        from dataclasses import asdict

        from app.services.llm.base import ToolCall, ToolTurn

        def plain(value: Any) -> Any:
            return asdict(value) if isinstance(value, ToolCall) else str(value)

        transcript = json.dumps({"messages": messages, "tools": [t.name for t in tools]}, default=plain, sort_keys=True)
        key = prompt_key(model=self.model, operation=operation, system=system, user=transcript, schema="tool_turn")
        path = self._path(key)
        self.calls += 1
        if self.mode == "replay":
            if not path.is_file():
                raise ReplayMissing(f"no recording {key[:12]} for model {self.model}; re-run with --live")
            self.replayed += 1
            saved = json.loads(path.read_text())["output"]
            turn = ToolTurn(
                text=saved["text"],
                tool_calls=[ToolCall(**c) for c in saved["tool_calls"]],
                input_tokens=saved["input_tokens"],
                output_tokens=saved["output_tokens"],
            )
            if on_text is not None and turn.text:
                await on_text(turn.text)
            return turn
        assert self.inner is not None
        turn = await self.inner.tool_turn(
            system=system, messages=messages, tools=tools, operation=operation, max_tokens=max_tokens, on_text=on_text
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"model": self.model, "operation": operation, "schema": "tool_turn", "output": asdict(turn)},
                       indent=2, sort_keys=True)
            + "\n"
        )
        return turn
