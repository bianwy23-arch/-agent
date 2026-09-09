"""Transactional state changes. User language interpretation belongs upstream.

This module checks structural invariants and source references; it does not claim
that a quoted user sentence semantically authorizes every model-proposed change.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import sqlite3
from uuid import uuid4


class InvalidChange(ValueError):
    pass


def money(value):
    if not isinstance(value, dict) or set(value) != {"amount", "currency"}:
        raise InvalidChange("budget requires amount and currency")
    if not isinstance(value["amount"], str):
        raise InvalidChange("amount must be a decimal string")
    try:
        amount = Decimal(value["amount"])
    except InvalidOperation as exc:
        raise InvalidChange("invalid amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise InvalidChange("amount must be finite and positive")
    if value["currency"] not in {"USD", "CNY"}:
        raise InvalidChange("unsupported currency")
    return amount


def validate_requirement(field, value):
    if not field or not isinstance(value, dict):
        raise InvalidChange("invalid requirement")
    if set(value) != {"status", "strength", "value"}:
        raise InvalidChange("requirement requires status, strength, value")
    if value["status"] not in {"active", "no_preference"}:
        raise InvalidChange("unknown requirement status")
    if value["strength"] not in {"hard", "soft"}:
        raise InvalidChange("unknown requirement strength")
    if value["status"] == "no_preference":
        if value["value"] is not None:
            raise InvalidChange("no_preference must have null value")
    elif field == "budget":
        money(value["value"])
    elif not isinstance(value["value"], str) or not value["value"].strip():
        raise InvalidChange("non-budget requirement requires nonempty text")


class TaskStore:
    """One SQLite transaction per group; instances must be closed by callers."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def close(self):
        self.db.close()

    def create(self, category):
        if not isinstance(category, str) or not category:
            raise InvalidChange("category required")
        task_id = str(uuid4())
        state = {"id": task_id, "category": category, "revision": 0,
                 "requirements_version": 0, "requirements": {}, "excluded": {},
                 "candidates": {}, "turns": {}, "history": [], "receipts": {},
                 "pending": {}, "exploration": None,
                 "decision": {"phase": "exploration", "focus_ids": [],
                              "shortlist_ids": [], "unresolved_tradeoffs": [],
                              "selection": None}, "displays": {}}
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, json.dumps(state)))
        return task_id

    def get(self, task_id):
        row = self.db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise InvalidChange("unknown task")
        return json.loads(row[0])

    def _save(self, state):
        self.db.execute("UPDATE tasks SET state = ? WHERE id = ?",
                        (json.dumps(state, allow_nan=False), state["id"]))

    def register_turn(self, task_id, turn_id, user_text):
        if not isinstance(turn_id, str) or not turn_id or not isinstance(user_text, str) or not user_text:
            raise InvalidChange("turn ID and user text required")
        with self.db:
            state = self.get(task_id)
            old = state["turns"].get(turn_id)
            if old is not None and old != user_text:
                raise InvalidChange("turn ID already bound to different input")
            state["turns"][turn_id] = user_text
            self._save(state)

    @staticmethod
    def _source(state, turn_id, quote):
        if not isinstance(quote, str) or not quote or quote not in state["turns"].get(turn_id, ""):
            raise InvalidChange("source quote must occur in the registered user turn")

    @staticmethod
    def _key(turn_id, group_id):
        if not isinstance(group_id, str) or not group_id:
            raise InvalidChange("group ID required")
        return json.dumps([turn_id, group_id])

    @staticmethod
    def _refresh(state, requirement_changed):
        state["revision"] += 1
        if requirement_changed:
            state["requirements_version"] += 1
        for candidate_id, candidate in state["candidates"].items():
            candidate["qualification"] = "unknown"
            candidate["user_excluded"] = candidate_id in state["excluded"]
        # Facts survive; conclusions must be evaluated against current conditions.
        state["decision"]["needs_reassessment"] = True

    def apply_group(self, task_id, turn_id, group_id, operations, quote, *, dependent=False):
        """All operations in a group succeed together or nothing is written.

        A caller can submit independent groups separately. Failed groups can be
        corrected; applied groups have immutable receipts and cannot be replayed
        with different contents. Unknown fields/conditions fail closed.
        """
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            key = self._key(turn_id, group_id)
            payload = {"operations": operations, "quote": quote, "dependent": dependent}
            if key in state["receipts"]:
                if state["receipts"][key] != payload:
                    raise InvalidChange("applied group ID reused with different payload")
                return "already_applied"
            if not isinstance(operations, list) or not operations or type(dependent) is not bool:
                raise InvalidChange("nonempty operations and boolean dependent required")
            writes = []
            targets = set()
            for op in operations:
                if not isinstance(op, dict) or set(op) != {"target", "key", "value"}:
                    raise InvalidChange("operation requires only target, key, value")
                target, field, value = op["target"], op["key"], op["value"]
                if target not in {"requirements", "excluded"} or not isinstance(field, str) or not field:
                    raise InvalidChange("unsupported target")
                if (target, field) in targets:
                    raise InvalidChange("conflicting writes to same field")
                targets.add((target, field))
                if target == "requirements" and value is not None:
                    validate_requirement(field, value)
                    value = {**deepcopy(value), "source": {"kind": "explicit", "turn_id": turn_id, "quote": quote}}
                if target == "excluded":
                    if field not in state["candidates"] or value not in (True, None) or (value is not None and type(value) is not bool):
                        raise InvalidChange("exclusion requires a known candidate and true or null")
                writes.append({"target": target, "key": field,
                               "before": deepcopy(state[target].get(field)), "after": value,
                               "reverted_by": None})
            for write in writes:
                self._write(state, write["target"], write["key"], write["after"])
            state["history"].append({"id": key, "turn_id": turn_id, "quote": quote,
                                     "dependent": dependent, "writes": writes, "undo_of": []})
            state["receipts"][key] = deepcopy(payload)
            state["pending"].pop(key, None)
            self._refresh(state, any(w["target"] == "requirements" for w in writes))
            self._save(state)
        return "applied"

    @staticmethod
    def _write(state, target, key, value):
        if value is None:
            state[target].pop(key, None)
        else:
            state[target][key] = deepcopy(value)

    def undo(self, task_id, turn_id, group_id, quote, *, field=None):
        """Undo last applied user turn, or the latest change to one requirement.

        A pending-only previous turn is not skipped to undo an older user turn.
        Partial undo of dependent groups is rejected for upstream clarification.
        """
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            key = self._key(turn_id, group_id)
            receipt = {"undo_field": field, "quote": quote}
            if key in state["receipts"]:
                if state["receipts"][key] != receipt:
                    raise InvalidChange("applied group ID reused")
                return "already_applied"
            previous = list(state["turns"])
            previous = previous[:previous.index(turn_id)]
            if not previous:
                raise InvalidChange("no prior turn")
            selected = []
            for event in reversed(state["history"]):
                if event["turn_id"] not in previous:
                    continue
                if field is None and event["turn_id"] != previous[-1]:
                    continue
                active = [w for w in event["writes"] if w["reverted_by"] is None]
                chosen = [w for w in active if field is None or (w["target"] == "requirements" and w["key"] == field)]
                if not chosen:
                    continue
                if event["dependent"] and len(chosen) != len(active):
                    raise InvalidChange("partial undo would split a dependent group")
                selected.append((event, chosen))
                if field is not None:
                    break
            if not selected:
                raise InvalidChange("no applied change in requested scope")
            writes = []
            for event, chosen in selected:
                for old in reversed(chosen):
                    target, name = old["target"], old["key"]
                    writes.append({"target": target, "key": name,
                                   "before": deepcopy(state[target].get(name)),
                                   "after": deepcopy(old["before"]), "reverted_by": None})
                    self._write(state, target, name, old["before"])
                    old["reverted_by"] = key
            state["history"].append({"id": key, "turn_id": turn_id, "quote": quote,
                                     "dependent": any(e["dependent"] for e, _ in selected),
                                     "writes": writes, "undo_of": [e["id"] for e, _ in selected]})
            state["receipts"][key] = receipt
            self._refresh(state, any(w["target"] == "requirements" for w in writes))
            self._save(state)
        return "applied"

    def clarify(self, task_id, turn_id, group_id, quote, fields):
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            key = self._key(turn_id, group_id)
            if key in state["receipts"]:
                raise InvalidChange("group already applied")
            if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or not f for f in fields):
                raise InvalidChange("clarification fields required")
            state["pending"][key] = {"quote": quote, "fields": fields}
            self._save(state)

    def cancel_clarification(self, task_id, source_turn, group_id):
        with self.db:
            state = self.get(task_id)
            state["pending"].pop(self._key(source_turn, group_id), None)
            self._save(state)

    def explore(self, task_id, turn_id, quote, overrides):
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            if not isinstance(overrides, dict) or not overrides:
                raise InvalidChange("nonempty exploration overrides required")
            for field, value in overrides.items():
                validate_requirement(field, value)
            state["exploration"] = {"turn_id": turn_id, "quote": quote,
                                    "overrides": deepcopy(overrides),
                                    "base_requirements_version": state["requirements_version"]}
            self._save(state)

    def end_exploration(self, task_id):
        with self.db:
            state = self.get(task_id)
            state["exploration"] = None
            self._save(state)

    def remember(self, task_id, product_id, facts):
        """Trusted catalog adapter only, never direct model-supplied facts."""
        if not isinstance(product_id, str) or not product_id or not isinstance(facts, dict):
            raise InvalidChange("candidate ID and facts required")
        with self.db:
            state = self.get(task_id)
            candidate = state["candidates"].setdefault(product_id, {"facts": {}})
            candidate["facts"].update(deepcopy(facts))
            candidate["qualification"] = "unknown"
            candidate["user_excluded"] = product_id in state["excluded"]
            self._save(state)

    def display(self, task_id, display_id, product_ids):
        with self.db:
            state = self.get(task_id)
            if not display_id or not product_ids or len(set(product_ids)) != len(product_ids):
                raise InvalidChange("display requires unique IDs")
            if any(pid not in state["candidates"] for pid in product_ids):
                raise InvalidChange("display contains unknown candidate")
            if display_id in state["displays"] and state["displays"][display_id] != product_ids:
                raise InvalidChange("display ID is immutable")
            state["displays"][display_id] = list(product_ids)
            self._save(state)

    def resolve(self, task_id, display_id, position):
        ids = self.get(task_id)["displays"].get(display_id)
        if ids is None or type(position) is not int or not 1 <= position <= len(ids):
            raise InvalidChange("unknown display or invalid position")
        return ids[position - 1]
