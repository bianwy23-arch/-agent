"""Explicit, bounded local runtime configuration; never print credentials."""
from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    root: Path
    api_key: str = field(repr=False)
    model: str = "deepseek-v4-flash"
    max_turns: int = 12
    max_tool_calls: int = 24
    request_timeout: float = 45
    turn_timeout: float = 150
    max_tokens: int = 4096
    max_retries: int = 1
    repeated_call_limit: int = 3

    @classmethod
    def load(cls, root):
        root = Path(root).resolve()
        values = {**dotenv_values(root / ".env"), **os.environ}
        key = values.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is missing; configure the local .env")
        def integer(name, default, low, high):
            value = int(values.get(name, default))
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            return value
        return cls(root=root, api_key=key, model=values.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                   max_turns=integer("AGENT_MAX_TURNS", 12, 1, 30),
                   max_tool_calls=integer("AGENT_MAX_TOOL_CALLS", 24, 1, 60),
                   request_timeout=integer("AGENT_REQUEST_TIMEOUT", 45, 5, 120),
                   turn_timeout=integer("AGENT_TURN_TIMEOUT", 150, 10, 600),
                   max_tokens=integer("AGENT_MAX_OUTPUT_TOKENS", 4096, 256, 8192),
                   max_retries=integer("AGENT_API_RETRIES", 1, 0, 2),
                   repeated_call_limit=integer("AGENT_REPEATED_CALL_LIMIT", 3, 2, 5))
