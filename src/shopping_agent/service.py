"""Business boundaries for model-controlled tools, without SDK dependencies."""
from copy import deepcopy
from decimal import Decimal
import json
import re
from uuid import uuid4

from .requirement_intent import validate_intent, IntentConflict, clear_absence
from .answer_delivery import answered_message, validate_visible_message
from .evidence import issue_evidence, validate_citation, expand_reason_references
from .presentation import claim_text
from .contracts import TurnPlan, FinalAnswer
from .live_limits import FACTS as LIVE_FACTS, verify_live_limit
from .state_facts import state_facts, verify_state_answer
from .delivery_progress import effective_count
from .quantity import effective as quantity_request, context as quantity_context, validate as validate_quantity
from .state import InvalidChange, money
from .qualification import predicate, normalize
from .assessment import assess_candidates as compute_assessment
from .delivery import validate_recommendation_reasons
from .delivery import comparison_view, feedback, product_cards, verify_claims, verify_proposal


class ShoppingTurn:
    def __init__(self, store, catalog, conversation, text):
        self.store, self.catalog, self.conversation = store, catalog, conversation
        self.text = text
        self.turn_id = str(uuid4())
        self.plan = None
        self.final = None
        self.group_errors = []
        self.decision_effects = {}
        self.searches = []
        self.inspected = {}
        self.evidence_registry = {}
        self.events = []
        self.out_of_scope = False
        self.display_snapshot = None
        self.applied_scopes = set()
        self.scene_before = {}
        self.hypotheses_before = {}
        self.failure_state_before = None
        self.discovery_before = None
        self.state_queries = []
        self.state_query_mode = None

    @property
    def task_id(self):
        return self.conversation.get("active_task_id")

    def state(self):
        return self.store.get(self.task_id) if self.task_id else None

    @staticmethod
    def _user_state(state):
        if not state:
            return {}
        # Exclude evidence, assessments, receipts and display bookkeeping.
        result = {key: deepcopy(state.get(key)) for key in
                  ('requirements', 'preference_relations', 'excluded', 'exploration',
                   'scenarios', 'hypotheses', 'pending', 'rejected_directions')}
        decision = state.get('decision', {})
        result['decision'] = {key: deepcopy(decision.get(key)) for key in
                              ('shortlist_ids', 'focus_ids', 'selection')}
        return result

    def failure_message(self):
        message = '本轮执行未完成。'
        if self.failure_state_before is not None:
            if self._user_state(self.state()) != self.failure_state_before:
                message += '本轮已写入的任务变更已保留。'
            else:
                message += '本轮未写入需求或选择变更。'
        if self.group_errors:
            quotes = list(dict.fromkeys(e['quote'] for e in self.group_errors if e.get('quote')))
            if quotes:
                message += '未完成的操作：' + '；'.join(quotes) + '。'
            if any(e.get('action') == 'explore' for e in self.group_errors):
                message += '本次临时方案创建未完成。'
        return message

    def process(self, plan: TurnPlan):
        from .state_queries import validate_queries, query_results
        validate_queries(self, plan.state_queries)
        if plan.state_queries:
            if self.state_queries and (plan.state_queries != self.state_queries or plan.state_query_mode != self.state_query_mode):
                raise InvalidChange('cannot change state queries during a turn')
            if self.plan is not None and not self.state_queries:
                raise InvalidChange('parse complete state queries on the first process_turn')
            if plan.state_query_mode == 'only':
                if any((plan.category, plan.new_task, plan.resume_task_id, plan.groups,
                        plan.scope_ids is not None, plan.stop_requested, plan.cheapest_requested,
                        plan.recommendation_scope, plan.question_response, plan.direction_rejections)):
                    raise InvalidChange('pure state query cannot route or modify tasks; use alongside for explicit operations')
            self.state_queries = list(plan.state_queries)
            self.state_query_mode = plan.state_query_mode
        if self.state_query_mode == 'only':
            if not plan.state_queries:
                raise InvalidChange('repeat the same pure state query plan')
            self.plan = plan
            return {'applied': [], 'errors': [], 'state_queries': query_results(self),
                    'instruction': 'Finish answered/state if all queries resolved; otherwise needs_user without question. Program renders all query fields. No product IDs, claims, state_fact_refs or extra tools.'}
        if self.out_of_scope:
            return {"applied": [], "errors": [], "blocking_issues": [{"code": "unsupported_category", "action": "data_limited"}], "state": self.state()}
        if self.plan is None and plan.category is not None and plan.category not in self.catalog.categories:
            self.plan = plan
            self.out_of_scope = True
            return {"applied": [], "errors": [], "blocking_issues": [{"code": "unsupported_category", "action": "data_limited", "supported_categories": self.catalog.categories}], "state": self.state()}
        if self.plan is not None:
            previous = self.plan.model_dump(exclude={"groups","question_response","direction_rejections","state_queries","state_query_mode"})
            supplied = plan.model_dump(exclude={"groups","question_response","direction_rejections","state_queries","state_query_mode"})
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
                self.store.record_question_response(self.task_id, self.turn_id, self.text)
                if plan.scope_ids is not None:
                    if any(pid not in self.state()["candidates"] for pid in plan.scope_ids):
                        raise InvalidChange("scope must refer to known candidates")
            before = self.state() or {}
            self.discovery_before = deepcopy(before)
            self.failure_state_before = self._user_state(before)
            self.scene_before = deepcopy(before.get('scenarios', {}))
            self.hypotheses_before = deepcopy(before.get('hypotheses', {}))
            self.requirements_before = deepcopy(before.get('requirements', {}))
            self.plan = plan
            self.display_snapshot = deepcopy(self.state()["displays"]) if self.task_id else {}
        failures = {e["group_id"]: e for e in self.group_errors}
        applied = []
        bound = {}
        exclusions, selections = {}, {}
        for group in plan.groups:
            try:
                ops = [op.model_dump(mode='json') for op in group.operations]
                for op in ops:
                    if op['target']=='requirements' and (op['key'] in self.catalog._products or str(op.get('value',{}).get('field','') if isinstance(op.get('value'),dict) else '') in self.catalog._products):
                        raise InvalidChange('product IDs are not requirement fields; exclusion uses action=apply, target=decision, key=exclude, value={product_ids:[ID],display_id:DISPLAY,positions:[]}; use scope=hypothetical for a temporary exclusion and preserve the existing exploration')
                    if op['target'] == 'decision':
                        value = op['value']
                        display = value.get('display_id')
                        if display and display not in self.display_snapshot:
                            raise InvalidChange('display was not visible before this turn')
                        if value.get('positions'):
                            if value.get('product_ids') or not display:
                                raise InvalidChange('positions require display and no explicit product_ids')
                            ids = self.display_snapshot[display]
                            if any(type(n) is not int or n<1 or n>len(ids) for n in value['positions']):
                                raise InvalidChange('invalid display position')
                            value['product_ids'] = [ids[n-1] for n in value['positions']]
                            value['positions'] = []
                        if op['key'] in {'select_tentative','select_confirmed','focus_set','shortlist_add','exclude'}:
                            bucket = exclusions if op['key']=='exclude' else selections
                            for pid in value.get('product_ids',[]):
                                bucket.setdefault((group.scope,pid),set()).add(group.group_id)
                    elif op['target']=='excluded' and op['value'] is True:
                        exclusions.setdefault((group.scope,op['key']),set()).add(group.group_id)
                bound[group.group_id] = ops
            except (InvalidChange, KeyError, TypeError, AttributeError) as exc:
                bound[group.group_id] = InvalidChange(str(exc))
        ambiguous = set()
        for product in exclusions.keys() & selections.keys():
            ambiguous.update(exclusions[product] | selections[product])
        for group in plan.groups:
            try:
                if not self.task_id:
                    raise InvalidChange("no active task")
                if not group.quote or group.quote not in self.text:
                    raise InvalidChange("quote must occur in current input")
                args = (self.task_id, self.turn_id, group.group_id)
                ops = bound[group.group_id]
                if isinstance(ops, Exception): raise ops
                # Creation validates against its formal base, before the overlay exists.
                # Ending an absent/stale overlay is safe; reads/writes still require it.
                validation_scope = 'formal' if group.action in {'explore', 'end_exploration'} else group.scope
                validate_intent(group, ops, self.requirements(validation_scope))
                if group.group_id in ambiguous:
                    self.store.clarify(*args, group.quote, ['decision'])
                    applied.append({'group_id':group.group_id, 'result':'pending', 'reason':'exclude_and_select_same_product'})
                    failures.pop(group.group_id, None)
                    continue
                from .discovery import budget as discovery_budget, effective
                budget_before_group = {scope: discovery_budget(effective(self.state(), scope) or {})
                                       for scope in ("formal", "hypothetical")}
                if group.action == "apply":
                    for op in ops:
                        if op['target']=='requirements' and isinstance(op.get('value'),dict) and op['value'].get('status')=='no_preference' and re.search(r'没(?:有)?(?:明确)?(?:说|提|表达)|尚未|还没|未明确',group.quote) and not re.search(r'现在.{0,8}(?:不在意|无所谓)',group.quote):
                            raise InvalidChange('absence of an explicit preference is unknown, not no_preference; remove this unintended preference operation from the SAME group and preserve existing state')
                        from .action_policy import budget_increase_refused
                        if op['target']=='requirements' and op['key']=='budget' and (plan.direction_rejections or budget_increase_refused(self.text)):
                            prior = self.state()['requirements'].get('budget')
                            value = op['value']
                            explicit_relaxation = re.search(r'取消预算|不设预算|预算不限|不限制预算|预算(?:改|调整|提高|增加|放宽)(?:为|到)',group.quote)
                            if prior and prior.get('status')=='active' and not explicit_relaxation:
                                loosens = value is None or value.get('status')!='active'
                                if not loosens and value.get('value',{}).get('currency')==prior['value']['currency']:
                                    loosens = Decimal(str(value['value']['amount']))>Decimal(str(prior['value']['amount']))
                                if loosens:
                                    preserved={k:v for k,v in prior.items() if k in {'status','strength','value'}}
                                    raise InvalidChange('refusing a budget increase preserves the existing hard ceiling; it does not cancel it. Correct the SAME group by retaining this authoritative budget: '+json.dumps(preserved))
                    if not ops:
                        if group.group_id in failures and not (
                                failures[group.group_id].get("code") == "requirement_intent_conflict"
                                and group.quote == failures[group.group_id].get("quote")
                                and group.interpretation and group.interpretation.kind == "no_requirement"
                                and clear_absence(group.quote)):
                            raise InvalidChange("an empty group cannot retract an unresolved failed change")
                        outcome = "no_change"
                    else:
                        outcome = self.store.apply_group(*args, ops, group.quote, dependent=group.dependent, scope=group.scope)
                elif group.action == "undo":
                    outcome = self.store.undo(*args, group.quote, field=group.undo_field, scope=group.scope)
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
                        raise InvalidChange("exploration accepts requirement overrides only. For temporary product decisions correct this SAME group to action=apply, scope=hypothetical, target=decision, key=exclude/shortlist_add/select_confirmed, value={product_ids:[ID],display_id:DISPLAY,positions:[]}. Do not replace the existing exploration or turn a product ID into a requirement")
                    self.store.explore(self.task_id, self.turn_id, group.quote, {op["key"]: op["value"] for op in ops})
                    outcome = "hypothetical"
                else:
                    self.store.end_exploration(self.task_id)
                    outcome = "ended_exploration"
                # Record executed budget transitions, not merely proposed operations.
                # Keep this across process_turn repairs in the same turn.
                actions = getattr(self, "discovery_budget_actions", {})
                for scope, previous_budget in budget_before_group.items():
                    current_budget = discovery_budget(effective(self.state(), scope) or {})
                    if current_budget != previous_budget:
                        actions[scope] = group.action
                self.discovery_budget_actions = actions
                self.applied_scopes.add("hypothetical" if group.action=="explore" else group.scope)
                entry = {"group_id": group.group_id, "result": outcome}
                key = self.store._key(self.turn_id, group.group_id)
                effect = deepcopy(self.state().get('decision_effects', {}).get(key)) if group.action == 'apply' else None
                if effect:
                    effect['replayed'] = outcome == 'already_applied'
                    self.decision_effects[key] = effect
                    entry.update(result=outcome if effect['replayed'] else effect['status'], effect=effect)
                applied.append(entry)
                failures.pop(group.group_id, None)
            except InvalidChange as exc:
                failures[group.group_id] = {"group_id": group.group_id, "error": str(exc), "quote": group.quote,
                                            "action": group.action, "scope": group.scope}
                if isinstance(exc, IntentConflict):
                    failures[group.group_id].update(code=exc.code, quote=group.quote, scope=group.scope, repair="reinterpret_same_group")
        self.group_errors = list(failures.values())
        if self.task_id:
            self.store.revalidate_decisions(self.task_id, self.catalog)
        state = self.state()
        budget = state["requirements"].get("budget") if state else None
        issues = []
        if budget and budget["status"] == "active" and budget["value"]["currency"] != "USD":
            issues.append({"code": "budget_currency", "action": "needs_user",
                           "message": "Ask for a USD budget; do not retry queries or convert implicitly."})
        unsupported = [field for field, requirement in (state["requirements"].items() if state else [])
                       if field != "budget" and requirement["status"] == "active" and requirement["strength"] == "hard"
                       and predicate(requirement.get("field") or field, requirement["value"]) is None]
        capabilities = []
        if unsupported:
            capabilities.append({"code": "unsupported_hard_predicate", "fields": unsupported,
                                 "recommendation_result": "data_limited",
                                 "instruction": "Requirements have been saved. The current verifier cannot prove these predicates; more inspect calls cannot establish eligibility. For recommendation requests explain this limitation now; for record-only requests acknowledge the saved state. Do not drop requirements, ask the user to supply product facts, or claim no_match."})
        if state:
            from .action_policy import scene_from_text, classify_gaps
            scene = scene_from_text(self.text)
            if scene and not plan.stop_requested and not any(g.action == 'undo' or any(o.target=='scenarios' and o.key=='carrying' for o in g.operations) for g in plan.groups):
                self.store.apply_group(self.task_id, self.turn_id, '__scene_carrying',
                    [{'target':'scenarios','key':'carrying','value':scene}], self.text)
                state = self.state()
            if plan.question_response:
                response = plan.question_response
                if response.quote not in self.text:
                    raise InvalidChange('question response must quote current user input')
                with self.store.db:
                    current = self.state()
                    question = next((q for q in reversed(current['question_history']) if q['question_key']==response.question_key), None)
                    if not question or (question.get('response') or {}).get('turn_id') != self.turn_id:
                        raise InvalidChange('response must refer to the preceding actual question; use question_history.question_key exactly, or omit question_response if no question was delivered')
                    if response.outcome == 'no_preference' and not any(any(r.get('field',k)==f and r.get('status')=='no_preference'
                            for k,r in current['requirements'].items()) for f in question.get('fields',[])):
                        raise InvalidChange('no_preference response requires corresponding explicit requirement updates')
                    question['response']['status'] = response.outcome
                    question['response']['no_preference_fields'] = [r.get('field',k) for k,r in current['requirements'].items() if r.get('status')=='no_preference' and r.get('source',{}).get('turn_id')==self.turn_id]
                    self.store._save(current)
                state = self.state()
            from .action_policy import budget_increase_refused
            for rejection in plan.direction_rejections:
                if rejection.quote not in self.text:
                    raise InvalidChange('direction rejection must quote current user input')
            if plan.direction_rejections or budget_increase_refused(self.text):
                directions = state.setdefault("rejected_directions", [])
                if "raise_budget" not in directions:
                    with self.store.db:
                        current = self.store.get(self.task_id)
                        if "raise_budget" not in current["rejected_directions"]:
                            current["rejected_directions"].append("raise_budget")
                            current.setdefault('rejected_direction_events',[]).append({'direction':'raise_budget','turn_id':self.turn_id,'quote':plan.direction_rejections[0].quote if plan.direction_rejections else self.text,'origin':'model' if plan.direction_rejections else 'rule'})
                            self.store._save(current)
                    state = self.state()
            self.store.revalidate_decisions(self.task_id,self.catalog)
            state = self.state()
            gaps = classify_gaps(state, None, list(self.inspected), self.text)
        else:
            gaps = []
        from .discovery import sync_requests, progress, preferred_scope
        sync_requests(self, self.discovery_before or {})
        state = self.state()
        discovery = {scope: progress(self, scope) for scope in self.active_scopes()} if state else {}
        from .decision_effects import decision_context
        return {"discovery": discovery, "recommendation_scope": preferred_scope(self) if state else 'formal',
                "decision_effects": list(self.decision_effects.values()),
                "decision_context": decision_context(state, self.catalog, events=True) if state else None,
                "applied": applied, "errors": self.group_errors, "blocking_issues": issues, "capability_issues": capabilities,
                "hypotheses": deepcopy((state or {}).get("hypotheses", {})), "gaps": gaps,
                "delivery_constraints":{"requested_count":effective_count(self),"quantity_request":quantity_request(self),"display_limit":10,"record_only":bool(re.search(r"只记录|仅记录|只保存|仅保存",self.text)),
                    "instruction":("Correct errors using the SAME failed group_id before finish; record_only does not bypass failed groups" if self.group_errors else ("record_only=true: finish {kind:answered,message:已记录,product_ids:[],claims:[],explanation_topics:[]}; do not inspect/recommend" if re.search(r"只记录|仅记录|只保存|仅保存",self.text) else "Complete the current request using relevant evidence; do not invent additional requirements"))},
                "capability_facts": LIVE_FACTS, "state": state, "state_facts": state_facts(self) if self.task_id else {},
                "state_queries": query_results(self) if self.state_queries else []}

    def _ready(self):
        if self.state_query_mode == "only":
            raise InvalidChange("pure state query does not permit product tools")
        if self.plan is None or self.task_id is None:
            raise InvalidChange("process_turn with a supported task is required first")
        if self.out_of_scope:
            raise InvalidChange("unsupported category; finish with data_limited without changing the current task")
        if self.plan.stop_requested:
            raise InvalidChange("user requested stop; do not use more tools")

    def active_scopes(self):
        state = self.state() or {}
        exploration = state.get('exploration')
        valid = exploration and exploration['base_requirements_version'] == state['requirements_version']
        return ['formal', 'hypothetical'] if valid else ['formal']

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

    def _intent(self, intent, action, product_id=None):
        from .contracts import ActionIntent
        from .action_policy import classify_gaps
        if intent is not None:
            intent = ActionIntent.model_validate(intent).model_dump()
        else:
            intent = {'gap_id':'supply:current' if action=='search' else 'evidence:uninspected',
                      'action':action,'expected_information':'current scoped evidence','direction':None}
        if intent['action'] not in ({'inspect','delivery_revalidation'} if action=='inspect' else {'search'}):
            raise InvalidChange('action intent does not match tool')
        if intent.get('direction') in self.state().get('rejected_directions',[]) or intent.get('direction')=='raise_budget':
            raise InvalidChange('search cannot relax a hard budget or reopen a rejected direction')
        assessment = next(reversed(self.state().get('assessments',{}).values()),None)
        gaps = classify_gaps(self.state(), assessment, list(self.inspected),self.text)
        gap = next((g for g in gaps if g['id']==intent['gap_id']),None)
        if intent['action']=='delivery_revalidation':
            if not product_id or product_id in self.inspected:
                raise InvalidChange('delivery_revalidation is one necessary check per product per turn')
        elif not gap or gap['status']!='open' or gap['kind'] not in ({'candidate_supply'} if action=='search' else {'product_evidence','capability'}):
            raise InvalidChange('action must target an open relevant supply/evidence/capability gap')
        return intent

    def search(self, query, scope, intent=None):
        self._ready()
        if self.plan.scope_ids is not None:
            raise InvalidChange("user limited this turn to existing products; inspect them instead")
        if scope not in {"formal", "hypothetical"}:
            raise InvalidChange("invalid search scope")
        intent = self._intent(intent,'search')
        from .action_policy import evidence_signature, record_action
        before = evidence_signature(self.state())
        from .discovery import progress
        expansion = progress(self, scope)
        needs_coverage = bool(expansion and not expansion['coverage_complete'])
        # A cheapest request needs one current exhaustive proof even if the pool
        # has not grown. Once recorded, normal no-progress limits apply again.
        active_budget = self.requirements(scope).get('budget', {}).get('value')
        needs_coverage |= bool(self.plan.cheapest_requested and not query and not any(
            result['scope'] == scope and result['coverage'] == 'exhaustive_structured_filter'
            and result['requirements_version'] == self.state()['requirements_version']
            and result['filters'].get('budget') == active_budget
            for result in self.searches))
        recent = [a for a in self.state().get('action_history',[]) if a['action']=='search'
                  and a['requirements_version']==self.state()['requirements_version']]
        if not needs_coverage and len(recent)>=2 and not recent[-1]['new_decision_evidence'] and recent[-1].get('after_evidence') == before:
            raise InvalidChange('search did not add decision evidence; finish or inspect')
        reqs = self.requirements(scope)
        budget = reqs.get("budget")
        value = budget["value"] if budget and budget["status"] == "active" and budget["strength"] == "hard" else None
        from .action_policy import note_progress
        with self.store.db:
            tracked = self.store.get(self.task_id)
            stagnant, _ = note_progress(tracked, "search")
            self.store._save(tracked)
        if stagnant and not needs_coverage:
            raise InvalidChange("search did not add decision evidence; finish or inspect instead of repeating a same-state query")
        result = self.catalog.search(self.state()["category"], budget=value, query=query,
                                     excluded_ids=self.state()["excluded"])
        retrieved_ids = [c["id"] for c in result["items"]]
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
            self.store.remember(self.task_id, card["id"], {'price':{'status':'source_reported','value':deepcopy(card['price']),'evidence':{'field':'price','text':str(card['price']['amount'])}}})
            self.store.assess(self.task_id, card["id"], self.catalog.qualification(card["id"], self.requirements("formal")))
        action = record_action(self.store,self.task_id,self.turn_id,intent,before,evidence_signature(self.state()),
            {'query':query,'scope':scope,'filters':deepcopy(result['filters']),'catalog_version':result['catalog_version'],'retrieved_ids':retrieved_ids,'coverage':result.get('coverage'),'returned_ids':[c['id'] for c in result['items']]})
        self.events.append({'event':'action_outcome',**action})
        result['action_outcome']=action
        result['discovery']=progress(self, scope)
        result["quantity_context"] = quantity_context(self, scope)
        self.searches.append(deepcopy(result))
        return result

    def assess_candidates(self, product_ids, scope, purpose):
        """Assess only candidates already known to this task.

        The returned ID is service-owned; model supplied scores, properties or
        budgets are never accepted as assessment input.
        """
        self._ready()
        if not isinstance(product_ids, list) or not product_ids:
            raise InvalidChange("product_ids required")
        if len(set(product_ids)) != len(product_ids):
            raise InvalidChange("product IDs must be unique")
        state = self.state()
        if self.plan.scope_ids is not None and set(product_ids) - set(self.plan.scope_ids):
            raise InvalidChange("candidate outside requested scope")
        if any(pid not in state["candidates"] for pid in product_ids):
            raise InvalidChange("assess_candidates requires searched or inspected candidates")
        requirements = self.requirements(scope)  # validates hypothetical authorization
        excluded = set(state["excluded"])
        if scope == "hypothetical":
            excluded.update(state["exploration"].get("excluded", {}))
        if set(product_ids) & excluded:
            raise InvalidChange("excluded products cannot enter the assessment")
        known_pool = list(state["candidates"])
        allowed = [pid for pid in known_pool if pid not in excluded and
                   (self.plan.scope_ids is None or pid in self.plan.scope_ids)]
        if purpose == "recommend" and set(allowed) - set(product_ids):
            raise InvalidChange("recommend assessment must cover the known pool in scope; missing: " +
                                ", ".join(sorted(set(allowed) - set(product_ids))))
        from .change_review import refresh_changed_candidates
        refreshed = refresh_changed_candidates(self, product_ids, scope)
        state = self.state()
        evidence = []
        for pid in product_ids:
            candidate = state["candidates"][pid]
            inspected = self.inspected.get(pid)
            if inspected is None:
                # Facts persisted by an earlier turn are usable for calculation,
                # but the delivery gate still requires current-turn inspection.
                saved = {k: v for k, v in candidate.get("facts", {}).items()
                         if k != "price" and isinstance(v, dict) and v.get("evidence")}
                normalized_saved = {k: normalize({"attributes": saved}, k) for k in saved}
                normalized_saved.update(candidate.get("normalized", {}))
                inspected = {"normalized": normalized_saved}
            normalized = deepcopy(inspected.get("normalized", {}))
            price_fact = inspected.get("facts", {}).get("price") or candidate.get("facts", {}).get("price")
            if price_fact and price_fact.get("status") == "source_reported":
                amount = price_fact.get("value", {}).get("amount")
                if amount is not None:
                    normalized["price"] = {"status": "known", "values": [{"value": str(amount), "unit": "USD"}],
                                            "evidence": [deepcopy(price_fact.get("evidence", {"field": "price", "text": str(amount)}))]}
            evidence.append({"id": pid, "normalized": normalized,
                             "hard_assessment": self.catalog.qualification(pid, requirements),
                             "price": deepcopy(candidate.get("facts", {}).get("price")),
                             "evidence_refs": deepcopy(candidate.get("facts", {}))})
        snapshot = deepcopy(state)
        snapshot["task_id"] = self.task_id
        snapshot["requirements"] = requirements
        snapshot["excluded"] = {pid: True for pid in excluded}
        if scope == "hypothetical":
            snapshot["decision"] = deepcopy(state["exploration"]["decision"])
        snapshot["scope_ids"] = list(self.plan.scope_ids) if self.plan.scope_ids is not None else None
        omitted = [pid for pid in known_pool if pid not in product_ids]
        snapshot["retrieval_coverage"] = {
            "category": state["category"], "scope": scope, "known_pool_ids": known_pool,
            "omitted_ids": omitted,
            "omission_reasons": {pid: ("user_excluded" if pid in excluded else
                "outside_user_scope" if self.plan.scope_ids is not None and pid not in self.plan.scope_ids
                else "comparison_subset") for pid in omitted},
            "query_refs": [{"query": s.get("query"), "coverage": s.get("coverage"),
                            "requirements_version": s.get("requirements_version")} for s in self.searches],
            "matched": "unknown", "returned": len(product_ids),
            "eligible": sum(not any(c["hard_assessment"].get(k) for k in ("violated", "unknown", "conflict")) for c in evidence),
            "inspected": sum(pid in self.inspected for pid in product_ids), "truncated": "unknown"}
        snapshot["assessment_context"] = {"scope_ids": self.plan.scope_ids,
            "focus_ids": (state["exploration"]["decision"] if scope == "hypothetical" else state["decision"])["focus_ids"], "known_pool_ids": known_pool,
            "excluded_ids": sorted(excluded), "requirements": requirements, "hypotheses":state.get("hypotheses", {})}
        result = compute_assessment(snapshot, evidence, scope, purpose)
        from .change_review import change_review
        result["change_review"] = change_review(self, scope)
        result["refreshed_historical_ids"] = refreshed
        from .assessment import _match
        proposed = {hid:h for hid,h in state.get('hypotheses',{}).items() if h['status']=='proposed'}
        result['hypotheses'] = deepcopy(proposed)
        result['hypothesis_matches'] = {c['id']:{hid:_match({'status':'active','field':h['proposed_soft_expression']['field'],
            'expression':{k:v for k,v in h['proposed_soft_expression'].items() if k!='field'}}, c['normalized'].get(h['proposed_soft_expression']['field'],{}))
            for hid,h in proposed.items()} for c in evidence}
        assessment_id = str(uuid4())
        result["assessment_id"] = assessment_id
        from .action_policy import classify_gaps
        result['action_gaps'] = classify_gaps(state,result,list(self.inspected),self.text)
        self.store.save_assessment(self.task_id, assessment_id, result)
        self.events.append({"event": "assessment_created", "assessment_id": assessment_id,
                            "task_id": self.task_id, "requirements_version": state["requirements_version"],
                            "evidence_version": state["candidate_evidence_version"], "examined_ids": product_ids,
                            "purpose": purpose, "scope": scope, "policy_version": result["policy_version"]})
        return result

    def inspect(self, product_id, fields, intent=None):
        self._ready()
        if self.plan.scope_ids is not None and product_id not in self.plan.scope_ids:
            raise InvalidChange("product outside requested comparison scope")
        if product_id not in self.state()["candidates"]:
            raise InvalidChange("search before inspecting an unknown product")
        from .action_policy import evidence_signature, record_action
        before = evidence_signature(self.state())
        known_fields = set(self.state()['candidates'][product_id].get('facts',{}))
        requested_fields = set(fields) | {'price'}
        revalidation = requested_fields <= known_fields and product_id not in self.inspected
        if intent is None and revalidation:
            intent = {'gap_id':'evidence:uninspected','action':'delivery_revalidation','expected_information':'current turn delivery source check'}
        if product_id in self.inspected and requested_fields <= set(self.inspected[product_id]['facts']):
            result = deepcopy(self.inspected[product_id])
            result['evidence_records'] = issue_evidence(self, product_id)
            result['cache_hit']=True
            self.events.append({'event':'inspection_cache_hit','product_id':product_id,'cost_kind':'cached'})
            return result
        if intent and intent.get('action')=='delivery_revalidation' and not revalidation:
            raise InvalidChange('new fields require inspect action, not delivery_revalidation')
        action_intent = self._intent(intent,'inspect',product_id)
        result = self.catalog.inspect(product_id, list(dict.fromkeys(["price", *fields, *(r.get("field",k) for k,r in self.state()["requirements"].items())])))
        result["formal_qualification"] = self.catalog.qualification(product_id, self.requirements("formal"))
        cached = self.inspected.setdefault(product_id, {**result, "facts": {}})
        cached["facts"].update(result["facts"])
        cached["normalized"] = deepcopy(result["normalized"])
        self.store.remember(self.task_id, product_id, result["facts"], normalized=result["normalized"])
        self.store.assess(self.task_id, product_id, result["formal_qualification"])
        self.store.revalidate_decisions(self.task_id,self.catalog)
        if revalidation:
            action_intent['action']='delivery_revalidation'
        action = record_action(self.store,self.task_id,self.turn_id,action_intent,before,evidence_signature(self.state()),
            {'product_id':product_id,'fields':sorted(requested_fields),'raw_source_read':True,
             'new_decision_evidence':False if revalidation else before!=evidence_signature(self.state())})
        self.events.append({'event':'action_outcome',**action})
        result['action_outcome']=action
        result['evidence_records'] = issue_evidence(self, product_id)
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
        from .presentation import recommendation
        return recommendation(self, answer)

    def _render_state_ack(self, scope):
        from .presentation import product_name, field_label
        lines = ["临时方案已记录：" if scope == "hypothetical" else "当前要求："]
        for key, req in self.requirements(scope).items():
            if req.get("status") == "no_preference":
                lines.append(f"- {field_label(req.get('field') or key)}：没有特别偏好。")
            elif key == "budget":
                value = req["value"]
                lines.append(f"- 预算：{value['amount']} {value['currency']}。")
            else:
                quote = (req.get("source") or {}).get("quote", "")
                lines.append(f"- {field_label(req.get('field') or key)}：{quote}。")
        if scope=='formal':
            state = self.state()
            for key, scene in state.get('scenarios', {}).items():
                prior = self.scene_before.get(key, {})
                if scene['active']:
                    line = '- 场景“' + scene['text'] + '”：生效。'
                elif prior.get('active'):
                    line = '- 场景“' + prior['text'] + '”：已停用。'
                elif scene.get('source', {}).get('turn_id') == self.turn_id:
                    line = '- 已记录场景说明：“' + scene['text'] + '”。'
                else:
                    continue
                if line not in lines:
                    lines.append(line)
            withdrawn = [hyp for key, hyp in state.get('hypotheses', {}).items()
                         if self.hypotheses_before.get(key, {}).get('status') in {'proposed', 'confirmed'}
                         and hyp.get('status') == 'invalidated'
                         and hyp.get('invalidated_by', '').startswith('scenarios:')]
            if withdrawn:
                lines.append('- 随场景变化撤回了相应推测；独立明确偏好保持不变。')
        state=self.state();ex=state.get('exploration')
        current=ex if scope=='hypothetical' and ex else state
        for pid in current.get('excluded',{}):lines.append('- 已排除：'+product_name(self.catalog, pid)+'。')
        for pid in current.get('decision',{}).get('shortlist_ids',[]):lines.append('- 已保留：'+product_name(self.catalog, pid)+'。')
        selection=(current.get('decision',{}).get('selection') or {})
        if selection.get('product_id'):
            lines.append('- '+('明确选定' if selection.get('commitment')=='confirmed' else '当前倾向')+'：'+product_name(self.catalog, selection['product_id'])+'。')
        if scope=='formal' and ex:
            for key,value in ex.get('overrides',{}).items():
                if key=='budget' and value.get('status')=='active':
                    lines.append('- 临时方案预算：'+str(value['value']['amount'])+' '+value['value']['currency']+'；未改变以上正式预算。')
        if state.get('pending'):lines.append('- 仍有待澄清修改，未确认的部分尚未生效。')
        return "\n".join(lines)

    def _assessment(self, answer):
        if not answer.assessment_id:
            if answer.recommendation_proposal or any(c.kind not in {"attribute_fact", "evidence_gap"} for c in answer.claims):
                raise InvalidChange("comparative delivery requires a current assessment_id")
            soft = [r for r in (self.state() or {}).get("requirements", {}).values()
                    if r.get("strength") == "soft" and r.get("status") == "active"
                    and (r.get("expression") or {}).get("kind") in {"minimize", "maximize", "target", "prefer_value"}]
            if answer.kind == "recommendation" and soft:
                raise InvalidChange("recommendation with computable soft preferences requires a current assessment; reassess first")
            return None
        assessment = self.store.get_assessment(self.task_id, answer.assessment_id)
        if assessment.get("scope") != answer.scope:
            raise InvalidChange("assessment scope does not match the submitted answer")
        if assessment.get("task_id") != self.task_id:
            raise InvalidChange("assessment does not belong to the current task")
        context = assessment.get("assessment_context")
        state = self.state()
        excluded = set(state["excluded"])
        if answer.scope == "hypothetical":
            excluded.update((state.get("exploration") or {}).get("excluded", {}))
        current = {"scope_ids": self.plan.scope_ids, "focus_ids": (state["exploration"]["decision"] if answer.scope == "hypothetical" else state["decision"])["focus_ids"],
                   "known_pool_ids": list(state["candidates"]), "excluded_ids": sorted(excluded),
                   "requirements": self.requirements(answer.scope), "hypotheses":state.get("hypotheses", {})}
        if context != current:
            raise InvalidChange("assessment scope or candidate pool is stale; reassess")
        if set(answer.product_ids) - set(assessment["examined_ids"]):
            raise InvalidChange("displayed products must belong to the assessment")
        if answer.kind == "recommendation":
            allowed = set(current["known_pool_ids"]) - excluded
            if self.plan.scope_ids is not None:
                allowed &= set(self.plan.scope_ids)
            if allowed - set(assessment["examined_ids"]):
                raise InvalidChange("recommendation must assess the known pool in scope")
        return assessment

    def finish(self, answer: FinalAnswer, *, validate_only=False):
        answer = expand_reason_references(self, answer)
        if self.plan is None:
            raise InvalidChange("process_turn must parse the complete user message first")
        if self.state_query_mode == 'only':
            from .state_queries import pure_query_answer
            result = pure_query_answer(self, answer)
            if validate_only:
                return None
            self.final = result
            return result
        if answer.kind=='answered' and not answer.claims and re.search(r"只记录|仅记录|只保存|仅保存",self.text) and self.applied_scopes=={'hypothetical'} and (self.state() or {}).get('exploration'):
            # Acknowledgement scope follows successfully applied operations, not a model default.
            answer=answer.model_copy(update={'scope':'hypothetical'})
        if answer.answer_purpose is not None and answer.kind != "answered":
            raise InvalidChange("answer_purpose is only valid for answered")
        if self.out_of_scope and (answer.kind != "data_limited" or answer.product_ids):
            raise InvalidChange("unsupported category requires data_limited without product claims")
        if answer.kind == "execution_limited":
            raise InvalidChange("execution_limited is assigned by the runtime only; missing user information (including USD budget) requires needs_user, missing evidence requires data_limited")
        if self.plan.stop_requested and answer.kind != "stopped":
            raise InvalidChange("user requested stopping")
        if re.search(r"只记录|仅记录|只保存|仅保存", self.text) and (answer.kind=='recommendation' or answer.product_ids):
            raise InvalidChange('record-only request: finish answered without products or explanation_topics')
        if '限制' in self.text and answer.explanation_topics and not any(word in self.text for word in ['软偏好','硬条件','选定','购买','历史价格']):
            raise InvalidChange('user asks about current limitations, not general concepts; finish data_limited with current scoped evidence')
        if len(set(answer.product_ids)) != len(answer.product_ids):
            raise InvalidChange("duplicate product IDs")
        if self.group_errors and answer.kind not in {"data_limited", "needs_user", "execution_limited"}:
            raise InvalidChange("unresolved update errors must be disclosed or corrected; do not retry answered. Correct these SAME failed group IDs with process_turn: "+json.dumps(self.group_errors,ensure_ascii=False))
        if not self.task_id and (answer.product_ids or (answer.kind not in {"needs_user", "stopped", "data_limited"} and not (answer.kind == "answered" and (answer.answer_purpose == "interaction" or (answer.answer_boundary and not answer.answer_boundary.product_ids))))):
            raise InvalidChange("No supported shopping task. For an unsupported category submit kind=data_limited with no product_ids; for an unclear category submit needs_user. Do not repeat answered or invent a supported task.")
        from .decision_effects import validate_review
        validate_review(self, answer)
        from .discovery import validate_delivery, complete
        discovery_checked = validate_delivery(self, answer)
        for pid in answer.product_ids:
            if pid not in self.state()["candidates"]:
                raise InvalidChange("unknown displayed product")
            if self.plan.scope_ids is not None and pid not in self.plan.scope_ids:
                raise InvalidChange("display outside user scope")
        cited = set()
        for cite in answer.citations:
            validate_citation(self, cite)
            cited.add(cite.product_id)
        if answer.kind == "recommendation":
            validate_quantity(self, answer)
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
        if self.task_id and answer.kind in {"recommendation", "answered"}:
            for pid in answer.product_ids:
                if pid in self.state()["excluded"]:
                    raise InvalidChange("excluded products cannot be presented as current recommendations")
        assessment = self._assessment(answer) if self.task_id else None
        if answer.kind in {"answered", "recommendation"} and len(answer.product_ids) >= 2:
            for field, phrase in (("weight", "重量差"), ("price", "价格差")):
                if phrase in self.text and not re.search(r"(?:不要|不用|无需)[^，。]*" + phrase, self.text):
                    if not any(c.kind == "numeric_difference" and c.field == field for c in answer.claims):
                        raise InvalidChange("user requested an actual difference; submit numeric_difference for " + field)
        if self.task_id and answer.kind in {'answered','recommendation'} and re.search(r'(?:请|先|然后|后)问我',self.text) and not re.search(r'不要.{0,4}问|不用.{0,4}问|别.{0,4}问',self.text):
            from .action_policy import classify_gaps
            available = [g for g in classify_gaps(self.state(),assessment,list(self.inspected),self.text) if g['kind'] in {'user_information','tradeoff'} and g['status']=='open']
            if available:
                raise InvalidChange('user explicitly requested a useful question; finish needs_user with question targeting '+', '.join(g['id'] for g in available))
        if self.task_id and answer.kind=='answered' and re.search(r'预算(?:提高|增加|降低|减少|放宽|收紧)(?:一?点|一些)',self.text) and not re.search(r'只记录|仅记录|只保存|仅保存',self.text):
            from .action_policy import classify_gaps
            missing=[g['id'] for g in classify_gaps(self.state(),assessment,list(self.inspected),self.text) if g['kind']=='user_information' and g['status']=='open']
            if missing:
                raise InvalidChange('current request has unresolved user information; finish needs_user with question gap_id '+', '.join(missing)+' instead of silently acknowledging the state')
        live_limit_verified = verify_live_limit(self, answer)
        state_answer_verified = verify_state_answer(self, answer)
        # Verified state prose has no product claims; absence queries need no product scope.
        self._verified_claims = [] if state_answer_verified else verify_claims(self, answer, assessment)
        validate_recommendation_reasons(self, answer, self._verified_claims)
        proposal = verify_proposal(self, answer, assessment)
        requested = gap = None
        if answer.kind == "needs_user" and self.task_id:
            from .action_policy import classify_gaps
            gaps = classify_gaps(self.state(), assessment, list(self.inspected), self.text)
            requested = answer.question
            # Preserve legacy budget clarification callers while recording the
            # same canonical key as the structured path.
            if requested is None:
                from .contracts import Question
                candidates = [g for g in gaps if g.get("kind") == "user_information"]
                if len(candidates) != 1:
                    raise InvalidChange("needs_user requires a question targeting a current user/tradeoff gap")
                requested = Question(gap_id=candidates[0]["id"], expected_information=candidates[0]["impact"])
            gap = next((g for g in gaps if g.get("id") == requested.gap_id), None)
            if not gap or gap["kind"] not in {"user_information", "tradeoff"} or gap["status"] != "open":
                raise InvalidChange("question does not target an open user/tradeoff gap; legal question gap IDs: " + json.dumps([g["id"] for g in gaps if g["kind"] in {"user_information","tradeoff"} and g["status"]=="open"]))
        if validate_only:
            # Shared recommendation preflight stops before rendering and writes.
            # The final stage runs the same checks again before publishing.
            return None
        result = answer.model_dump(mode="json")
        if answer.kind == "recommendation":
            result["message"] = self._render_recommendation(answer)
            recommendation_base = result['message']
            from .change_review import render_change_review, change_review
            explanation = render_change_review(self, answer)
            result["change_review"] = change_review(self, answer.scope)
            if explanation:
                result["message"] += "\n\n" + explanation
            from .presentation import field_label
            for item in (proposal or {}).get("verified_emphasis", []):
                explanations = {"advantage": "排在前面的商品在这一点更符合你的偏好。",
                    "tradeoff": "排在前面的商品在这一点有所妥协，但在另一项偏好上更合适。",
                    "equal": "这些商品在这一点相同，可以结合其他需求选择。",
                    "unknown": "现有资料还不能比较这一点，不能据此判断谁更好。",
                    "strict": "目前按你指定的优先顺序推荐。"}
                result["message"] += "\n" + field_label(item["field"]) + "：" + explanations[item["reason"]]
        elif answer.kind == "no_match":
            # This terminal kind already requires a current exhaustive empty filter.
            # Render its bounded conclusion without unverified suggestions in draft prose.
            result['message'] = '这里收录的商品中没有符合你当前要求的款式。你的条件保持不变；其他商家仍可能有合适的商品。'
            if 'raise_budget' in self.state().get('rejected_directions',[]):
                result['message'] += '\n我会保留你的预算，不再建议提高。'
        elif answer.kind == "answered":
            result["message"] = answered_message(self, answer, self._verified_claims, state_answer_verified, live_limit_verified)
        elif answer.kind == 'needs_user' and gap and gap['kind']=='tradeoff':
            from .delivery import LABELS
            fields='、'.join(LABELS.get(f,f) for f in gap['fields'])
            result['message'] = '\n'.join(claim_text(self, c) for c in self._verified_claims)
            result['message'] += ('\n' if result['message'] else '') + '这几款各有取舍，你更重视'+fields+'中的哪一点？'
        elif answer.kind == "needs_user" and self.task_id and any("budget" in pending["fields"] for pending in self.state()["pending"].values()):
            result["message"] = "请确认这次希望采用的预算上限，提供明确的美元金额。待澄清的预算变更暂不生效。"
            budget = self.state()["requirements"].get("budget")
            if budget and budget["status"] == "active":
                result["message"] += "\n当前已生效预算仍为 " + budget["value"]["amount"] + " " + budget["value"]["currency"] + "。"
            excluded = self.state()["excluded"]
            if excluded:
                result["message"] += "\n当前排除仍保留：" + "；".join(self.catalog._products[pid]["title"] for pid in excluded) + "。"
        elif answer.kind == "data_limited" and self.task_id and (
                self.requirements(answer.scope).get("budget", {}).get("status") == "active" and
                self.requirements(answer.scope)["budget"].get("value", {}).get("currency") != "USD"):
            budget = self.requirements(answer.scope)["budget"]["value"]
            result["message"] = (f"已保留预算 {budget['amount']} {budget['currency']}。商品目录使用历史 USD 价格，"
                                 "当前不能做未经授权的汇率换算，因此无法判定哪些商品符合该预算，本轮不作推荐。"
                                 "这不代表商品库中没有匹配商品。")
            if any(q.get("question_key") == "user:budget" for q in self.state()["question_history"]):
                result["message"] += "已记录此前的预算问题和你的回应，本轮不重复追问。"
        elif answer.kind == "data_limited" and self.task_id and not self.out_of_scope:
            hard = [r for key, r in self.requirements(answer.scope).items() if key != "budget" and r["status"] == "active" and r["strength"] == "hard"]
            if hard:
                quotes = list(dict.fromkeys(r.get("source", {}).get("quote", str(r["value"])) for r in hard))
                result["message"] = ("现有资料还不能确认哪些商品满足：\n" + "\n".join("- " + q for q in quotes)
                                     + "\n\n我会保留这些要求，暂不推荐无法确认的商品；其他商品仍可能符合。")
                if any(predicate(r.get("field") or key, r["value"]) is None for key, r in self.requirements(answer.scope).items() if key != "budget" and r["status"] == "active" and r["strength"] == "hard"):
                    result["message"] += "\n这项要求需要进一步核实，现有商品描述不足以确认。"
                if answer.product_ids:
                    result["message"] += "\n以下仅为待核实参考，非主推荐：\n" + "\n".join(self.catalog._products[pid]["title"] for pid in answer.product_ids)
            else:
                from .delivery import render_evidence_limit
                result['message'] = render_evidence_limit(self, answer, self._verified_claims)
        if answer.kind == 'data_limited' and self.task_id and not self.out_of_scope:
            state = self.state()
            scope_ids = self.plan.scope_ids if self.plan.scope_ids is not None else list(state['candidates'])
            checks = [self.catalog.qualification(pid,self.requirements(answer.scope)) for pid in scope_ids if pid not in state['excluded']]
            if checks and all(c['violated'] for c in checks):
                result['message'] = '目前查看的商品都不符合你的必需条件，暂不推荐；这不代表其他商品也不符合。'
            if 'raise_budget' in state.get('rejected_directions',[]):
                # Free draft prose cannot reopen an expressly rejected direction.
                result['message'] = '我会保留你的预算，不再建议提高。现有资料还无法确认合适的商品，其他商品仍可能符合。'
        if self.task_id and not self.out_of_scope and not state_answer_verified and answer.kind == 'data_limited' and '限制' in self.text:
            from .delivery import current_limits
            result['message'] = current_limits(self,answer.scope)
            if self._verified_claims:
                result['message'] += '\n' + '\n'.join(claim_text(self, c) for c in self._verified_claims)
        if answer.kind == 'data_limited' and re.search(r'cups?|杯',self.text,re.I) and re.search(r'换算|升|liters?',self.text,re.I):
            result['message'] += '\n仅有 Cups 标注不足以确定升数；必须先确认杯制或同一容量的升数标注，并区分生米、熟饭与容器容量，不能把6 Cups直接当作1.5升。'
        result.update({"turn_id": self.turn_id, "task_id": self.task_id, "display_id": None,
                       "price_notice": "商品价格为历史 USD 数据，不代表当前报价或库存。",
                       "group_errors": self.group_errors})
        if answer.kind == "answered":
            validate_visible_message(result["message"])
        display_id = None
        shown = list(answer.product_ids)
        if answer.kind == 'answered' and assessment and not shown:
            # Verified comparisons themselves identify the displayed operands.
            # Persist one authoritative display for table, cards and all buttons.
            referenced = {pid for claim in self._verified_claims for pid in claim.get('product_ids',[])}
            shown = [pid for pid in assessment['examined_ids'] if pid in referenced]
        card_ids = list(shown)
        if assessment and answer.kind in {'recommendation', 'data_limited', 'answered'}:
            card_ids = list(dict.fromkeys(shown + assessment['examined_ids']))
        card_ids = [pid for pid in card_ids
                    if not any(self.qualification(pid, answer.scope)[key]
                               for key in ('violated', 'unknown', 'conflict'))]
        if card_ids:
            display_id = str(uuid4())
            self.store.display(self.task_id, display_id, card_ids,scope=answer.scope)
            result["display_id"] = display_id
            result["product_ids"] = shown
        result["claims"] = self._verified_claims
        result["recommendation_proposal"] = proposal
        result["product_cards"] = product_cards(self, card_ids, display_id, answer.scope, [c["field"] for c in self._verified_claims if c.get("field")]) if card_ids else []
        for card in result["product_cards"]:
            card["is_recommended"] = answer.kind == 'recommendation' and card['product_id'] in answer.product_ids
        result["comparison_view"] = comparison_view(self, assessment, self._verified_claims, shown, display_id)
        current_decision = ((self.state() or {}).get('exploration') or {}).get('decision',{}) if answer.scope=='hypothetical' else (self.state() or {}).get('decision',{})
        result["selection_ack"] = deepcopy(current_decision.get("selection"))
        if answer.scope=='hypothetical' and not state_answer_verified:
            result['message']='这是临时方案，你原来的要求和选择不变。\n'+result['message']
        result["feedback"] = feedback(self.state(), answer, assessment) if self.task_id else []
        if result["feedback"] and answer.answer_purpose != "interaction" and not state_answer_verified and answer.kind in {"recommendation", "answered"}:
            # Audit feedback remains available in JSON/logs. Publish only actionable caveats.
            if assessment and any(g.get('status') in {'unknown', 'conflict', 'unsupported'} for g in assessment.get('gaps', [])):
                result['message'] += '\n部分偏好还缺少足够资料，暂不能判断是否满足。'
            if (current_decision.get('selection') or {}).get('validity') == 'needs_review':
                result['message'] += '\n你之前选的商品仍保留，但需要重新确认是否符合现在的要求。'
            elif any('当前展示中' in note for note in result['feedback']):
                result['message'] += '\n这里提到的部分商品不符合现在的必需条件，仅供比较，暂不推荐。'
            elif any('保留项' in note for note in result['feedback']):
                result['message'] += '\n部分备选已不符合现在的要求，比较时请留意。'

        if requested is not None:
            self.store.record_question(self.task_id, self.turn_id, requested.model_dump(), gap, result["message"])
            result["question"] = requested.model_dump()
        self.events.append({"event": "delivery_checked", "assessment_id": answer.assessment_id,
                            "kind": answer.kind, "product_ids": shown, "stale": False})
        if answer.answer_purpose == "interaction":
            result.update(feedback=[], price_notice="")
        if live_limit_verified:
            result.update(message=answer.message, feedback=[], price_notice="")
        if answer.kind == "answered":
            validate_visible_message(result["message"])
        complete(self, answer, result, discovery_checked)
        if answer.kind == 'recommendation':
            from .presentation import recommendation
            # Preserve scope, changes, required comparisons and state caveats,
            # while removing every optional model-written recommendation reason.
            self.conservative_recommendation_message = result['message'].replace(
                recommendation_base, recommendation(self, answer, include_optional_reasons=False), 1)
        result["state"] = self.state()
        from .state_queries import append_queries
        append_queries(self, result)
        from .decision_effects import attach
        attach(self, result)
        self.final = result
        return result
