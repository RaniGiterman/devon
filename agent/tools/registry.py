"""Tool registry in OpenAI function-calling format."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

MAX_RESULT_CHARS = 3500  # tool output is re-sent on every loop step; keep it lean


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Awaitable[str]]

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    async def call(self, name: str, arguments: str | dict) -> tuple[str, float]:
        t0 = time.time()
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool {name!r}. Available: {', '.join(self.tools)}", 0.0
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"error: invalid arguments: {e}", 0.0
        allowed = set(tool.parameters.get("properties", {}))
        args = {k: v for k, v in args.items() if k in allowed}
        try:
            result = await tool.fn(**args)
        except TypeError as e:
            result = f"error: bad arguments for {name}: {e}"
        except Exception as e:  # tools must never crash the loop
            result = f"error: {type(e).__name__}: {e}"
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + f"\n... [truncated {len(result) - MAX_RESULT_CHARS} chars]"
        return result, time.time() - t0


def obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}
