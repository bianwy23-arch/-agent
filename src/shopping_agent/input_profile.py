"""Numeric composition of actual outbound chat requests, including HTTP retries.

Characters use decoded Unicode JSON with compact separators, not model tokens.
No prompts, product text, arguments, headers, or credentials are retained.
"""
import json
from collections import Counter


def json_chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def profile_request(body):
    payload = json.loads(body)
    messages = payload.get("messages", [])
    roles = Counter()
    tool_parts = Counter()
    for message in messages:
        roles[message["role"]] += json_chars(message)
        if message["role"] == "tool":
            try:
                content = json.loads(message.get("content") or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(content, dict):
                for key, value in content.items():
                    bucket = key if key in {"state", "execution_budget", "items", "comparisons", "pairwise"} else "other"
                    tool_parts[bucket] += json_chars(value)
    return {"wire_bytes": len(body), "request_json_chars": json_chars(payload),
            "messages_json_chars": json_chars(messages),
            "message_chars_by_role": dict(roles),
            "tools_json_chars": json_chars(payload.get("tools", [])),
            "other_json_chars": json_chars({k:v for k,v in payload.items() if k not in {"messages", "tools"}}),
            "message_count": len(messages), "tool_count": len(payload.get("tools", [])),
            "tool_value_chars_by_component": dict(tool_parts)}


def summarize_inputs(rows):
    records = [record for row in rows for record in row.get("request_inputs", [])]
    roles, parts = Counter(), Counter()
    for record in records:
        roles.update(record["message_chars_by_role"])
        parts.update(record["tool_value_chars_by_component"])
    return {"scope": "actual HTTP attempts including retries; Unicode JSON characters, not tokens; component values exclude JSON key syntax and are not additive to message totals",
            "attempts": len(records), "turns_with_profile": sum(bool(r.get("request_inputs")) for r in rows),
            "total_request_json_chars": sum(r["request_json_chars"] for r in records),
            "max_request_json_chars": max((r["request_json_chars"] for r in records), default=None),
            "total_tools_json_chars": sum(r["tools_json_chars"] for r in records),
            "message_chars_by_role": dict(roles), "tool_value_chars_by_component": dict(parts)}
