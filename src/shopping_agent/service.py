"""Business boundaries for model-controlled tools, without SDK dependencies."""
from copy import deepcopy
from decimal import Decimal
import json
import re
from uuid import uuid4

from .contracts import TurnPlan, FinalAnswer
from .state import InvalidChange, money
from .qualification import predicate


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
        self.out_of_scope = False

    @property
    def task_id(self):
        return self.conversation.get("active_task_id")

    def state(self):
        return self.store.get(self.task_id) if self.task_id else None

    def process(self, plan: TurnPlan):
        if self.out_of_scope:
            return {"applied": [], "errors": [], "blocking_issues": [{"code": "unsupported_category", "action": "data_limited"}], "state": self.state()}
        if self.plan is None and plan.category is not None and plan.category not in self.catalog.categories:
            self.plan = plan
            self.out_of_scope = True
            return {"applied": [], "errors": [], "blocking_issues": [{"code": "unsupported_category", "action": "data_limited", "supported_categories": self.catalog.categories}], "state": self.state()}
        if self.plan is not None:
            previous = self.plan.model_dump(exclude={"groups"})
            supplied = plan.model_dump(exclude={"groups"})
            # Retry calls may omit/default routing metadata. The first route stays
            # authoritative; a correction cannot create or switch another task.
            for key, value in supplied.items():
                if value is not None and value is not False and value != previous[key]:
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
                    if not ops:
                        if group.group_id in failures:
                            raise InvalidChange("an empty group cannot retract an unresolved failed change")
                        outcome = "no_change"
                    else:
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
        unsupported = [field for field, requirement in (state["requirements"].items() if state else [])
                       if field != "budget" and requirement["status"] == "active" and requirement["strength"] == "hard"
                       and predicate(field, requirement["value"]) is None]
        capabilities = []
        if unsupported:
            capabilities.append({"code": "unsupported_hard_predicate", "fields": unsupported,
                                 "recommendation_result": "data_limited",
                                 "instruction": "Requirements have been saved. The current verifier cannot prove these predicates; more inspect calls cannot establish eligibility. For recommendation requests explain this limitation now; for record-only requests acknowledge the saved state. Do not drop requirements, ask the user to supply product facts, or claim no_match."})
        return {"applied": applied, "errors": self.group_errors, "blocking_issues": issues, "capability_issues": capabilities, "state": state}

    def _ready(self):
        if self.plan is None or self.task_id is None:
            raise InvalidChange("process_turn with a supported task is required first")
        if self.out_of_scope:
            raise InvalidChange("unsupported category; finish with data_limited without changing the current task")
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
        # Examine all structured matches. Unknown/conflicting candidates remain
        # visible; only proven violations are excluded from eligible candidates.
        checked = []
        eliminated = []
        for card in result["items"]:
            assessment = self.catalog.qualification(card["id"], reqs)
            card["hard_checks"] = {k: v for k, v in assessment.items() if k != "checks"}
            card["qualification"] = "violated" if assessment["violated"] else ("unknown" if assessment["unknown"] or assessment["conflict"] else "satisfied")
            if assessment["violated"]:
                eliminated.append({"id": card["id"], "violated": assessment["violated"]})
            else:
                checked.append(card)
        checked.sort(key=lambda c: (c["qualification"] != "satisfied", Decimal(str(c["price"]["amount"])), c["id"]))
        result["items"] = checked
        result["eliminated"] = eliminated
        result["hard_requirements_evaluated"] = True
        result["other_requirements_evaluated"] = True
        for card in result["items"]:
            self.store.remember(self.task_id, card["id"], {})
            self.store.assess(self.task_id, card["id"], self.catalog.qualification(card["id"], self.requirements("formal")))
        self.searches.append(deepcopy(result))
        return result

    def inspect(self, product_id, fields):
        self._ready()
        if self.plan.scope_ids is not None and product_id not in self.plan.scope_ids:
            raise InvalidChange("product outside requested comparison scope")
        if product_id not in self.state()["candidates"]:
            raise InvalidChange("search before inspecting an unknown product")
        result = self.catalog.inspect(product_id, list(dict.fromkeys(["price", *fields, *self.state()["requirements"]])))
        result["formal_qualification"] = self.catalog.qualification(product_id, self.requirements("formal"))
        cached = self.inspected.setdefault(product_id, {**result, "facts": {}})
        cached["facts"].update(result["facts"])
        self.store.remember(self.task_id, product_id, result["facts"])
        self.store.assess(self.task_id, product_id, result["formal_qualification"])
        return result

    def qualification(self, product_id, scope):
        """Deterministic source-based assessment, separate from inspection access."""
        result = self.catalog.qualification(product_id, self.requirements(scope))
        if product_id in self.state()["excluded"]:
            result["violated"].append("user_excluded")
        inspected = self.inspected.get(product_id)
        if not inspected:
            result["unknown"].append("not_inspected_this_turn")
            return result
        return result

    def _render_recommendation(self, answer):
        """User-visible decisive claims come from checked facts, not draft prose.

        Agent still chooses candidates and tools. Rendering does not choose a
        product, infer a preference, or bypass the qualification gate.
        """
        labels = {"connectivity":"连接方式", "connection":"连接方式", "form_factor":"佩戴形式",
                  "weight":"商品重量", "waterproof":"防水等级", "speaker_type":"音箱类型",
                  "keyboard_description":"键盘类型", "number_of_keys":"按键数量",
                  "compatible_devices":"兼容设备", "tracking":"追踪方式", "number_of_buttons":"按键数量",
                  "capacity":"容量", "power":"功率", "wattage":"功率", "voltage":"电压",
                  "material":"材料", "power_source":"供电方式", "vacuum_type":"吸尘器形式",
                  "battery_life":"续航", "light_source":"光源", "age_range":"适用年龄",
                  "shaving_use":"剃须用途", "head_type":"刀头类型"}
        scope = "临时假设方案" if answer.scope == "hypothetical" else "当前正式需求"
        lines = []
        if re.search(r"(?:替我|直接).*(?:下单|购买)|帮我下单", self.text):
            lines.append("本项目只能提供导购建议，不能替您下单、支付或完成购买。")
        lines.append("按" + scope + "，以下商品通过了已记录硬条件的核验：")
        for position, pid in enumerate(answer.product_ids, 1):
            inspected = self.inspected[pid]
            amount = inspected["facts"]["price"]["value"]["amount"]
            lines.extend(["", str(position) + ". **" + inspected["context"]["title"] + "**", "   历史价格：" + str(amount) + " 美元。"])
            assessment = self.qualification(pid, answer.scope)
            for field, detail in assessment["checks"].items():
                if field == "budget":
                    lines.append("   - 价格符合已记录的美元预算上限。")
                else:
                    source = detail["attribute"]["evidence"][0]
                    lines.append("   - " + labels.get(field, field) + "：资料标注“" + source["text"] + "”，符合该项要求。")
        if self.plan.cheapest_requested:
            lines.extend(["", "最便宜的比较范围仅为当前品类的冻结商品库及有效条件。"])
        lines.extend(["", "以上依据商品资料标注，未进行独立实测；价格为历史 USD 快照，不代表当前报价或库存。"])
        return "\n".join(lines)

    def finish(self, answer: FinalAnswer):
        if self.plan is None:
            raise InvalidChange("process_turn must parse the complete user message first")
        if self.out_of_scope and (answer.kind != "data_limited" or answer.product_ids):
            raise InvalidChange("unsupported category requires data_limited without product claims")
        if answer.kind == "execution_limited":
            raise InvalidChange("execution_limited is assigned by the runtime only; missing user information (including USD budget) requires needs_user, missing evidence requires data_limited")
        if self.plan.stop_requested and answer.kind != "stopped":
            raise InvalidChange("user requested stopping")
        if len(set(answer.product_ids)) != len(answer.product_ids):
            raise InvalidChange("duplicate product IDs")
        if self.group_errors and answer.kind not in {"data_limited", "needs_user", "execution_limited"}:
            raise InvalidChange("unresolved update errors must be disclosed")
        if not self.task_id and (answer.product_ids or answer.kind not in {"needs_user", "stopped", "data_limited"}):
            raise InvalidChange("No supported shopping task. For an unsupported category submit kind=data_limited with no product_ids; for an unclear category submit needs_user. Do not repeat answered or invent a supported task.")
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
            # Narrow, explicit user count only. No default quota or inferred count.
            counts = {"一个": 1, "一款": 1, "两个": 2, "两款": 2, "三个": 3, "三款": 3}
            explicit = re.search(r"(?:推荐|只给)(一个|一款|两个|两款|三个|三款)", self.text)
            if explicit and len(answer.product_ids) != counts[explicit[1]]:
                raise InvalidChange("user explicitly requested exactly " + str(counts[explicit[1]]) + " products")
            if not answer.product_ids:
                raise InvalidChange("recommendation requires products")
            if self.state()["pending"]:
                raise InvalidChange("pending requirement changes prevent final recommendation")
            for pid in answer.product_ids:
                eligibility = self.qualification(pid, answer.scope)
                if eligibility["violated"] or eligibility["unknown"] or eligibility["conflict"]:
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
        if answer.kind == "recommendation":
            result["message"] = self._render_recommendation(answer)
        elif answer.kind == "needs_user" and self.task_id and any("budget" in pending["fields"] for pending in self.state()["pending"].values()):
            result["message"] = "请确认这次希望采用的预算上限，提供明确的美元金额。待澄清的预算变更暂不生效。"
            budget = self.state()["requirements"].get("budget")
            if budget and budget["status"] == "active":
                result["message"] += "\n当前已生效预算仍为 " + budget["value"]["amount"] + " " + budget["value"]["currency"] + "。"
            excluded = self.state()["excluded"]
            if excluded:
                result["message"] += "\n当前排除仍保留：" + "；".join(self.catalog._products[pid]["title"] for pid in excluded) + "。"
        elif answer.kind == "data_limited" and self.task_id and not self.out_of_scope:
            hard = [r for key, r in self.requirements(answer.scope).items() if key != "budget" and r["status"] == "active" and r["strength"] == "hard"]
            if hard:
                quotes = list(dict.fromkeys(r.get("source", {}).get("quote", str(r["value"])) for r in hard))
                result["message"] = ("当前查询与证据不足以确认商品同时满足这些硬条件：\n" + "\n".join("- " + q for q in quotes)
                                     + "\n\n这些要求已保留，不会按已满足来主推荐。这不代表全市场无匹配；这里的范围仅为当前冻结商品资料与已核验信息。")
                if any(predicate(key, r["value"]) is None for key, r in self.requirements(answer.scope).items() if key != "budget" and r["status"] == "active" and r["strength"] == "hard"):
                    result["message"] += "\n现有功能暂不能可靠核实这类条件，不能只凭商品描述把它判为满足。"
                if answer.product_ids:
                    result["message"] += "\n以下仅为待核实参考，非主推荐：\n" + "\n".join(self.catalog._products[pid]["title"] for pid in answer.product_ids)
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
