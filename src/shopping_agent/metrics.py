"""Offline aggregation of numeric-only runtime metrics (never provider billing)."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path


def distribution(values):
    values = sorted(values)
    return {"count": len(values), **{name: values[max(0, math.ceil(q*len(values))-1)] if values else None
            for name, q in (("p50", .5), ("p95", .95), ("max", 1))}}


def summarize(path, prices=None):
    rows, invalid, duplicates, seen = [], [], 0, set()
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        try:
            row = json.loads(line)
            if row.get("schema_version") != 1 or not all(k in row for k in
                    ("turn_id", "model", "usage", "usage_complete", "stop_reason", "model_calls", "tools", "total_seconds", "queue_seconds", "elapsed_seconds")):
                raise ValueError("invalid metric record")
            if row["turn_id"] in seen:
                duplicates += 1
                continue
            seen.add(row["turn_id"])
            rows.append(row)
        except (ValueError, TypeError, AttributeError):
            invalid.append(number)
    # Correlate only identifiers/events from the sibling trace, never copy its content.
    starts, ends, trace_invalid = set(), set(), 0
    trace_path = Path(path).with_name("trace.jsonl")
    if trace_path.exists():
        for line in trace_path.read_text().splitlines():
            try:
                event = json.loads(line)
                if event.get("event") == "turn_start":
                    starts.add(event["turn_id"])
                elif event.get("event") == "turn_end":
                    ends.add(event["turn_id"])
            except (ValueError, TypeError, KeyError, AttributeError):
                trace_invalid += 1
    usage = {key: sum(row["usage"].get(key, 0) for row in rows)
             for key in ("requests", "input_tokens", "output_tokens", "total_tokens")}
    report = {"turns": len(rows), "invalid_lines": invalid, "duplicate_turns": duplicates,
              "scope": "completed metric records; sibling trace identifiers used to detect unfinished/missing summaries",
              "trace_available": trace_path.exists(), "trace_invalid_lines": trace_invalid,
              "unfinished_trace_turns": len(starts - ends), "trace_turns_missing_metrics": len(starts - seen),
              "stop_reasons": dict(Counter(row["stop_reason"] for row in rows)),
              "models": dict(Counter(row["model"] for row in rows)), "usage": usage,
              "incomplete_usage_turns": sum(not row["usage_complete"] for row in rows),
              "cache_unknown_turns": sum(row.get("cached_input_tokens") is None for row in rows),
              "known_cached_input_tokens": sum(row.get("cached_input_tokens") or 0 for row in rows),
              "timings": {key: distribution([row[key] for row in rows]) for key in
                          ("queue_seconds", "elapsed_seconds", "total_seconds")},
              "model_seconds": distribution([m["duration_seconds"] for row in rows for m in row["model_calls"]]),
              "tool_seconds": distribution([t["duration_seconds"] for row in rows for t in row["tools"]]),
              "tool_statuses": dict(Counter(t["status"] for row in rows for t in row["tools"])),
              "counts": {key: sum(row.get(key, 0) for row in rows) for key in
                         ("inspection_cache_hits", "schema_rejections", "business_rejections", "finalization_retries", "provider_http_attempts", "provider_http_responses", "provider_retries")}}
    from .input_profile import summarize_inputs
    report["input_composition"] = summarize_inputs(rows)
    report["model_queue_seconds"] = distribution([row["model_queue_seconds"] for row in rows if "model_queue_seconds" in row])
    estimate, cost_turns = 0, 0
    if prices:
        required = ("model", "version", "currency", "input_per_million", "cached_input_per_million", "output_per_million")
        if any(k not in prices for k in required):
            raise ValueError("price file missing required fields")
        if any(not isinstance(prices[k], (float,int)) or not math.isfinite(prices[k]) or prices[k]<0 for k in required[3:]):
            raise ValueError("prices must be finite non-negative numbers")
        for row in rows:
            cached = row.get("cached_input_tokens")
            if row["model"] != prices["model"] or not row["usage_complete"] or cached is None:
                continue
            u = row["usage"]
            if not 0 <= cached <= u["input_tokens"]:
                continue
            estimate += ((u["input_tokens"]-cached)*prices["input_per_million"] + cached*prices["cached_input_per_million"] + u["output_tokens"]*prices["output_per_million"])/1e6
            cost_turns += 1
    report["cost"] = {"estimated_known_amount": estimate if cost_turns else None,
                      "priced_turns": cost_turns, "complete": bool(rows) and cost_turns == len(rows),
                      "price_table": prices, "note": "configured estimate, not billed amount; unknown turns excluded"}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metrics", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prices", type=Path)
    args = parser.parse_args()
    report = summarize(args.metrics, json.loads(args.prices.read_text()) if args.prices else None)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    (args.output/"report.md").write_text("# Runtime statistics\n\nNumeric metadata only. Costs are estimates; missing usage/cache remains unknown.\n\n```json\n"+json.dumps(report, ensure_ascii=False,indent=2)+"\n```\n")
    print(json.dumps({"turns":report["turns"],"invalid_lines":report["invalid_lines"],"output":str(args.output)}))


if __name__ == "__main__":
    main()
