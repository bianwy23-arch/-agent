"""Business boundaries for model-controlled tools, without SDK dependencies."""
from copy import deepcopy
from decimal import Decimal
import json
from uuid import uuid4

from .contracts import TurnPlan, FinalAnswer
from .state import InvalidChange, money


class ShoppingTurn:
    def __init__(self, store, catalog, conversation, text):
        self.store, self.catalog, self.conversation = store, catalog, conversation
        self.text = text
        self.turn_id = str(uuid4())
        self.plan = None
        self.final = None
        self.group_errors = []
        self.searches = []
        self.inspected = {}
        self.events = []

    @property
    def task_id(self):
        return self.conversation.get("active_task_id")

    def state(self):
        return self.store.get(self.task_id) if self.task_id else None

    def process(self, plan: TurnPlan):
        if self.plan is not None:
            previous = self.plan.model_dump(exclude={"groups"})
            if plan.model_dump(exclude={"groups"}) != previous:
                raise InvalidChange("cannot change routing or interaction scope during a turn")
        else:
            if plan.resume_task_id:
                if plan.resume_task_id not in self.conversation["task_ids"]:
                    raise InvalidChange("task does not belong to this conversation")
                self.conversation["active_task_id"] = plan.resume_task_id
            elif plan.new_task or self.task_id is None:
                if plan.category not in self.catalog.categories:
                    if plan.category is not None:
                        raise InvalidChange("unsupported category; supported categories provided in context")
                    if plan.groups:
                        raise InvalidChange("category must be clarified before applying task changes")
                else:
                    tid = self.store.create(plan.category)
                    self.conversation["task_ids"].append(tid)
                    self.conversation["active_task_id"] = tid
            elif plan.category and plan.category != self.state()["category"]:
                raise InvalidChange("a different category requires a new task")
            if self.task_id:
                self.store.register_turn(self.task_id, self.turn_id, self.text)
                if plan.scope_ids is not None:
                    if any(pid not in self.state()["candidates"] for pid in plan.scope_ids):
                        raise InvalidChange("scope must refer to known candidates")
            self.plan = plan
        failures = {e["group_id"]: e for e in self.group_errors}
        applied = []
        for group in plan.groups:
            try:
                if not self.task_id:
                    raise InvalidChange("no active task")
                if not group.quote or group.quote not in self.text:
                    raise InvalidChange("quote must occur in current input")
                args = (self.task_id, self.turn_id, group.group_id)
                ops = [op.model_dump(mode="json") for op in group.operations]
                if group.action == "apply":
                    outcome = self.store.apply_group(*args, ops, group.quote, dependent=group.dependent)
                elif group.action == "undo":
                    outcome = self.store.undo(*args, group.quote, field=group.undo_field)
                elif group.action == "clarify":
                    self.store.clarify(*args, group.quote, group.clarification_fields)
                    outcome = "pending"
                elif group.action == "cancel_clarification":
                    if not group.pending_turn_id or not group.pending_group_id:
                        raise InvalidChange("pending turn and group IDs required")
                    self.store.cancel_clarification(self.task_id, group.pending_turn_id, group.pending_group_id)
                    outcome = "cancelled"
                elif group.action == "explore":
                    if any(op["target"] != "requirements" or op["value"] is None for op in ops):
                        raise InvalidChange("exploration accepts requirement overrides only")
                    self.store.explore(self.task_id, self.turn_id, group.quote, {op["key"]: op["value"] for op in ops})
                    outcome = "hypothetical"
                else:
                    self.store.end_exploration(self.task_id)
                    outcome = "ended_exploration"
                applied.append({"group_id": group.group_id, "result": outcome})
                failures.pop(group.group_id, None)
            except InvalidChange as exc:
                failures[group.group_id] = {"group_id": group.group_id, "error": str(exc)}
        self.group_errors = list(failures.values())
        state = self.state()
        budget = state["requirements"].get("budget") if state else None
        issues = []
        if budget and budget["status"] == "active" and budget["value"]["currency"] != "USD":
            issues.append({"code": "budget_currency", "action": "needs_user",
                           "message": "Ask for a USD budget; do not retry queries or convert implicitly."})
        return {"applied": applied, "errors": self.group_errors, "blocking_issues": issues, "state": state}

    def _ready(self):
        if self.plan is None or self.task_id is None:
            raise InvalidChange("process_turn with a supported task is required first")
        if self.plan.stop_requested:
            raise InvalidChange("user requested stop; do not use more tools")

    def requirements(self, scope):
        state = self.state()
        result = deepcopy(state["requirements"])
        if scope == "hypothetical":
            exploration = state["exploration"]
            if not exploration:
                raise InvalidChange("no temporary exploration")
            if exploration["base_requirements_version"] != state["requirements_version"]:
                raise InvalidChange("temporary exploration is stale; reassess before use")
            result.update(exploration["overrides"])
        return result

    def search(self, query, scope):
        self._ready()
        if self.plan.scope_ids is not None:
            raise InvalidChange("user limited this turn to existing products; inspect them instead")
        if scope not in {"formal", "hypothetical"}:
            raise InvalidChange("invalid search scope")
        reqs = self.requirements(scope)
        budget = reqs.get("budget")
        value = budget["value"] if budget and budget["status"] == "active" and budget["strength"] == "hard" else None
        result = self.catalog.search(self.state()["category"], budget=value, query=query,
                                     excluded_ids=self.state()["excluded"])
        result["scope"] = scope
        result["requirements_version"] = self.state()["requirements_version"]
        for card in result["items"]:
            self.store.remember(self.task_id, card["id"], {})
        self.searches.append(deepcopy(result))
        return result

    def inspect(self, product_id, fields):
        self._ready()
        if self.plan.scope_ids is not None and product_id not in self.plan.scope_ids:
            raise InvalidChange("product outside requested comparison scope")
        if product_id not in self.state()["candidates"]:
            raise InvalidChange("search before inspecting an unknown product")
        result = self.catalog.inspect(product_id, list(dict.fromkeys(["price", *fields])))
        cached = self.inspected.setdefault(product_id, {**result, "facts": {}})
        cached["facts"].update(result["facts"])
        self.store.remember(self.task_id, product_id, result["facts"])
        return result

    def qualification(self, product_id, scope):
        """Only budget and explicit product exclusions are automatic.

        Any other hard requirement remains unknown. No model-supplied 'passed'
        flag can turn it into an unconditional recommendation.
        """
        result = {"violated": [], "unknown": []}
        if product_id in self.state()["excluded"]:
            result["violated"].append("user_excluded")
        inspected = self.inspected.get(product_id)
        if not inspected:
            result["unknown"].append("not_inspected_this_turn")
            return result
        for name, req in self.requirements(scope).items():
            if req["status"] != "active" or req["strength"] != "hard":
                continue
            if name == "budget":
                value = req["value"]
                if value["currency"] != "USD":
                    result["unknown"].append("budget_currency")
                elif Decimal(str(inspected["facts"]["price"]["value"]["amount"])) > money(value):
                    result["violated"].append("budget")
            else:
                # Semantic hard constraints need a separate verified adapter.
                result["unknown"].append(name)
        return result

    def finish(self, answer: FinalAnswer):
        if self.plan is None:
            raise InvalidChange("process_turn must parse the complete user message first")
        if answer.kind == "execution_limited":
            raise InvalidChange("execution_limited is assigned by the runtime only; missing user information (including USD budget) requires needs_user, missing evidence requires data_limited")
        if self.plan.stop_requested and answer.kind != "stopped":
            raise InvalidChange("user requested stopping")
        if len(set(answer.product_ids)) != len(answer.product_ids):
            raise InvalidChange("duplicate product IDs")
        if self.group_errors and answer.kind not in {"data_limited", "needs_user", "execution_limited"}:
            raise InvalidChange("unresolved update errors must be disclosed")
        if not self.task_id and (answer.product_ids or answer.kind not in {"needs_user", "stopped", "data_limited"}):
            raise InvalidChange("no supported shopping task")
        for pid in answer.product_ids:
            if pid not in self.state()["candidates"]:
                raise InvalidChange("unknown displayed product")
            if self.plan.scope_ids is not None and pid not in self.plan.scope_ids:
                raise InvalidChange("display outside user scope")
        cited = set()
        for cite in answer.citations:
            inspected = self.inspected.get(cite.product_id)
            if not inspected:
                raise InvalidChange("citation requires evidence inspected this turn")
            raw = inspected["context"]
            if cite.field == "price":
                value = inspected["facts"]["price"]["evidence"]["text"]
            elif cite.field.startswith("details."):
                value = raw["details"].get(cite.field[8:])
            else:
                value = raw.get(cite.field)
            if value is None or not cite.quote.strip() or cite.quote not in str(value):
                raise InvalidChange("citation quote does not occur in its source field")
            cited.add(cite.product_id)
        if answer.kind == "recommendation":
            if not answer.product_ids:
                raise InvalidChange("recommendation requires products")
            if self.state()["pending"]:
                raise InvalidChange("pending requirement changes prevent final recommendation")
            for pid in answer.product_ids:
                eligibility = self.qualification(pid, answer.scope)
                if eligibility["violated"] or eligibility["unknown"]:
                    raise InvalidChange("unverified hard constraints: " + json.dumps(eligibility))
                if pid not in cited:
                    raise InvalidChange("each recommended product needs a source citation")
            if self.plan.cheapest_requested:
                coverage = [s for s in self.searches if s["scope"] == answer.scope and
                            s["coverage"] == "exhaustive_structured_filter" and
                            s["requirements_version"] == self.state()["requirements_version"]]
                if not coverage:
                    raise InvalidChange("cheapest requires exhaustive structured search")
                recommended_prices = [Decimal(str(self.inspected[pid]["facts"]["price"]["value"]["amount"])) for pid in answer.product_ids]
                for card in coverage[-1]["items"]:
                    if Decimal(str(card["price"]["amount"])) < max(recommended_prices):
                        q = self.qualification(card["id"], answer.scope)
                        if not q["violated"]:
                            raise InvalidChange("cheaper candidate still qualifies or has unresolved evidence")
        if answer.kind == "no_match":
            if not any(s["coverage"] == "exhaustive_structured_filter" and not s["items"] and
                       s["scope"] == answer.scope and s["requirements_version"] == self.state()["requirements_version"]
                       for s in self.searches):
                raise InvalidChange("no_match requires an exhaustive empty structured filter")
        result = answer.model_dump(mode="json")
        result.update({"turn_id": self.turn_id, "task_id": self.task_id, "display_id": None,
                       "price_notice": "商品价格为历史 USD 数据，不代表当前报价或库存。",
                       "group_errors": self.group_errors})
        if answer.product_ids:
            display_id = str(uuid4())
            self.store.display(self.task_id, display_id, answer.product_ids)
            result["display_id"] = display_id
        result["state"] = self.state()
        self.final = result
        return result
