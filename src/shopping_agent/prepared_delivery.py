"""Turn-local recommendation material and item repair, above the existing verifier."""
from copy import copy, deepcopy
import json
import re
from uuid import uuid4
from pydantic import Field
from .contracts import Contract, FinalAnswer, RecommendationReason, RecommendationProposal, Citation, Claim
from .state import InvalidChange, TaskStore
from .discovery import preferred_scope, record_only
from .evidence import issue_evidence, resolve_reference, expand_reason_references
from .delivery import validate_recommendation_reasons, verify_claims


class PreparedReason(Contract):
    facts: list[str] = Field(min_length=1, max_length=4)
    text: str = Field(min_length=1, max_length=180)


class PreparedItem(Contract):
    item: str
    reasons: list[PreparedReason] = Field(default_factory=list, max_length=2)
    fact_selections: list[str] | None = Field(default=None, description="Select source-bound display facts from display_facts; omit reasons. Program carries text and provenance together.")


class PreparedDelivery:
    def __init__(self, turn, call=None, emit=None):
        self.turn = turn
        self.emit = emit
        self.call = call or (lambda name, args, action: action())
        self.bundle = None
        self.order = None
        self.accepted = {}
        self.pending = set()
        self.completed = None
        self.submitted = None
        # Turn-wide: replacing prepared material must not restart the prose repair loop.
        self.reason_rounds = 0

    def signature(self):
        t = self.turn
        s = t.state()
        return json.dumps([t.turn_id, t.task_id, t.catalog.version, t.plan.model_dump(),
            t._user_state(s), s['requirements_version'], s['candidate_evidence_version'],
            s['candidates'], s.get('discovery_requests'), t.searches], sort_keys=True, default=str)

    def prepare(self, product_ids):
        t = self.turn
        t._ready()
        if self.completed or t.final:
            raise InvalidChange('recommendation already delivered')
        if record_only(t):
            raise InvalidChange('record-only request: finish answered without recommendation')
        scope = preferred_scope(t)
        budget = t.requirements(scope).get('budget', {})
        if budget.get('status') == 'active' and budget.get('value', {}).get('currency') != 'USD':
            # The catalog capability is known before inspecting any candidates.
            # Reuse the authoritative limitation renderer; never switch to formal.
            return t.finish(FinalAnswer(kind='data_limited', message='', scope=scope))
        if not product_ids or len(product_ids) > 10 or len(set(product_ids)) != len(product_ids):
            raise InvalidChange('prepare 1 to 10 distinct known products')
        scope = preferred_scope(t)
        t.requirements(scope)
        s = t.state()
        excluded = set(s['excluded']) | (set(s['exploration']['excluded']) if scope == 'hypothetical' else set())
        pool = [p for p in s['candidates'] if p not in excluded and (t.plan.scope_ids is None or p in t.plan.scope_ids)]
        if set(product_ids) - set(pool):
            raise InvalidChange('prepare products must belong to current allowed pool')
        # Explicit replacement invalidates the previous draft even if preparation fails.
        self.bundle = None
        self.order = None
        self.accepted = {}
        self.pending = set()
        from .change_review import change_review
        history = [c['product_id'] for c in change_review(t, scope).get('historical_candidates', [])
                   if c['qualification'] == 'satisfied' and c['product_id'] in pool]
        for pid in dict.fromkeys([*product_ids, *history]):
            fields = list(dict.fromkeys(['price', *t.catalog._products[pid]['attributes'],
                *(r.get('field') or k for k, r in t.requirements(scope).items())]))
            args = {'product_id': pid, 'fields': fields, 'intent': None}
            result = self.call('inspect_product', args, lambda pid=pid, fields=fields: t.inspect(pid, fields))
            if result.get('error'):
                raise InvalidChange('preparation inspection failed: ' + result['error'])
        args = {'product_ids': pool, 'scope': scope, 'purpose': 'recommend'}
        assessment = self.call('assess_candidates', args, lambda: t.assess_candidates(**args))
        if assessment.get('error'):
            raise InvalidChange('preparation assessment failed: ' + assessment['error'])
        prefix = uuid4().hex[:8]
        mapping, items, unavailable = {}, [], []
        for i, pid in enumerate(dict.fromkeys([*product_ids, *history]), 1):
            q = t.qualification(pid, scope)
            if any(q[k] for k in ('violated', 'unknown', 'conflict')):
                unavailable.append({'product_id': pid, 'qualification': q})
                continue
            item = f'{prefix}.p{i}'
            facts, refs = [], {}
            for j, record in enumerate(issue_evidence(t, pid), 1):
                key = f'{item}.{record["attribute"]}.{j}'
                refs[key] = record['evidence_id']
                facts.append({'fact': key, 'attribute': record['attribute'], 'text': record['excerpt'],
                              'truncated': record['excerpt_truncated']})
            # Full source blocks only: never truncate away a qualifier or ask the
            # model to independently reconstruct a reason and its citation.
            from .answer_boundary import evidence_status
            display_facts = []
            for fact in facts:
                source = resolve_reference(t, refs[fact['fact']], pid)
                if (len(source['text']) <= 600 and source['text'].strip()
                        and evidence_status(t, pid, fact['attribute']) not in {'conflict', 'missing'}):
                    display_facts.append({'fact': fact['fact'], 'attribute': fact['attribute'],
                                          'text': source['text']})
            mapping[item] = {'pid': pid, 'refs': refs, 'display_facts': display_facts}
            items.append({'item': item, 'product_id': pid, 'title': t.catalog._products[pid]['title'], 'facts': facts, 'display_facts': display_facts})
        self.bundle = {'scope': scope, 'assessment': assessment, 'mapping': mapping, 'signature': self.signature()}
        t.events.append({'event': 'recommendation_prepared', 'scope': scope, 'products': list(mapping),
                         'assessment_id': assessment['assessment_id']})
        return {'items': items, 'unavailable': unavailable, 'scope': scope,
                'assessment': {k: assessment.get(k) for k in ['preferences','pairwise','representatives','action_gaps']},
                'instruction': 'All submitted items are main recommendations and count toward the requested quantity. Select items in desired order with fact_selections from display_facts and empty reasons; submit_recommendation(items). Display facts are attributed catalog source reports, not independently verified performance. Do not send response_text for this path. Use each item own fact keys. Scope, assessment and citations are managed by the program. Reprepare to add or replace products.'}

    def current(self):
        if not self.bundle:
            raise InvalidChange('prepare_recommendation first')
        if self.signature() != self.bundle['signature']:
            raise InvalidChange('prepared recommendation is stale; prepare_recommendation again')

    def reason(self, item):
        mapping = self.bundle['mapping']
        if item.item not in mapping:
            raise InvalidChange('item outside prepared material; reprepare to select another product')
        record = mapping[item.item]
        if item.fact_selections is not None:
            if item.reasons:
                raise InvalidChange('fact_selections and free-text reasons cannot be mixed')
            allowed = {f['fact'] for f in record['display_facts']}
            if len(set(item.fact_selections)) != len(item.fact_selections) or set(item.fact_selections) - allowed:
                raise InvalidChange('choose distinct display_facts belonging to this item')
            for key in item.fact_selections:
                resolve_reference(self.turn, record['refs'][key], record['pid'])
        reasons = []
        for reason in item.reasons:
            if re.search(r'最(?:轻|重|便宜|贵|好|优|适合|划算)|比.{0,20}更', reason.text):
                raise InvalidChange('item reasons describe this product only; comparative or superlative claims require computed comparisons, not local facts. State supported product facts and conditional relevance.')
            if len(set(reason.facts)) != len(reason.facts) or set(reason.facts) - set(record['refs']):
                raise InvalidChange('use distinct facts belonging to this item: ' + ', '.join(record['refs']))
            reasons.append(RecommendationReason(product_id=record['pid'], text=reason.text,
                evidence_refs=[record['refs'][key] for key in reason.facts]))
        a = expand_reason_references(self.turn, FinalAnswer(kind='recommendation', message='',
            scope=self.bundle['scope'], product_ids=[record['pid']], recommendation_reasons=reasons))
        verified = verify_claims(self.turn, a, self.bundle['assessment'])
        validate_recommendation_reasons(self.turn, a, verified)
        result = item.model_dump()
        if item.fact_selections is not None:
            # All supplied references were validated above. Only the display
            # budget is trimmed; no invalid reference is silently repaired.
            # Price is already rendered authoritatively on every product row.
            result['fact_selections'] = [key for key in item.fact_selections
                if resolve_reference(self.turn, record['refs'][key], record['pid'])['field'] != 'price'][:2]
        return result

    def proposal(self, ids):
        a = self.bundle['assessment']
        proposal = RecommendationProposal(assessment_id=a['assessment_id'], ordered_ids=ids)
        # Emphasis describes computed relations, not a model-invented score.
        from .delivery import _match
        from .assessment import _cmp_for, _strict_order
        prefs = {p['id']: p for p in a.get('preferences', [])}
        eligible = {h['product_id'] for h in a['hard_assessments'] if h['status'] == 'satisfied'}
        for key in a.get('emphasis_ids', []):
            comparisons = {p: _cmp_for(prefs[key], _match(a, ids[0], key), _match(a, p, key)) for p in eligible - {ids[0]}}
            values = list(comparisons.values())
            if _strict_order(list(prefs.values()), a.get('preference_relations', [])): why = 'strict'
            elif 1 in values and -1 not in values: why = 'advantage'
            elif all(v == 0 for v in values): why = 'equal'
            elif None in values: why = 'unknown'
            else: why = 'tradeoff'
            proposal.preference_refs.append(key)
            proposal.emphasis_reasons[key] = why
        return proposal

    def answer(self, order=None, accepted=None):
        ids, reasons, citations, source_claims = [], [], [], []
        for key in (self.order if order is None else order):
            item = PreparedItem.model_validate((self.accepted if accepted is None else accepted)[key])
            record = self.bundle['mapping'][key]
            ids.append(record['pid'])
            # Revalidate at assembly as well as submission; preflight has no facts.
            if item.fact_selections is not None:
                item = PreparedItem.model_validate(self.reason(item))
            for key in item.fact_selections or []:
                source = resolve_reference(self.turn, record['refs'][key], record['pid'])
                source_claims.append(Claim(key='prepared_fact_' + key, kind='attribute_fact',
                    product_ids=[record['pid']], field=source['field']))
                citations.append(Citation(product_id=record['pid'], field=source['source_field'], quote=source['text']))
            for reason in item.reasons:
                reasons.append(RecommendationReason(product_id=record['pid'], text=reason.text,
                    evidence_refs=[record['refs'][f] for f in reason.facts]))
            # Price provenance is always needed, even when the selected reason is another fact.
            ref = next(ref for ref in record['refs'].values() if resolve_reference(self.turn, ref, record['pid'])['field'] == 'price')
            source = resolve_reference(self.turn, ref, record['pid'])
            citations.append(Citation(product_id=record['pid'],field=source['source_field'],quote=source['text']))
        claims = source_claims
        import re
        for field, phrase in [('price','价格差'),('weight','重量差')]:
            if len(ids)>=2 and phrase in self.turn.text and not re.search(r'(?:不要|不用|无需)[^，。]*'+phrase,self.turn.text):
                claims.append(Claim(key='difference_'+field,kind='numeric_difference',product_ids=ids[:2],field=field))
        return FinalAnswer(kind='recommendation',message='',scope=self.bundle['scope'],product_ids=ids,
            assessment_id=self.bundle['assessment']['assessment_id'],recommendation_proposal=self.proposal(ids),
            recommendation_reasons=reasons,citations=citations,claims=claims)

    def selection(self, items, repair=False):
        self.current()
        keys = [i.item for i in items]
        modes = {i.fact_selections is not None for i in items}
        if len(modes) > 1:
            raise InvalidChange('use one delivery mode for all submitted items')
        if not keys or len(set(keys)) != len(keys):
            raise InvalidChange('submit distinct prepared items')
        if set(keys) - set(self.bundle['mapping']):
            raise InvalidChange('select only prepared items; prepare again for new products')
        if repair:
            if self.order is None or set(keys) - self.pending:
                raise InvalidChange('repair only rejected items; accepted items cannot be rewritten')
            return self.order
        if self.order is not None:
            raise InvalidChange('draft exists; repair_recommendation only rejected items, or reprepare to change selection')
        return keys

    def preflight(self, items, repair=False):
        """Run the same final validator without optional prose or observable writes."""
        order = self.selection(items, repair)
        baseline = {key: {'item': key, 'reasons': []} for key in order}
        self.stage(self.answer(order, baseline))
        return order

    def stage(self, answer, commit=False):
        # One authoritative validator/renderer for preflight and final delivery.
        before = deepcopy(self.turn.state())
        snapshots = {self.turn.task_id: before}
        if self.turn.state_queries:
            snapshots.update({tid: self.turn.store.get(tid) for tid in self.turn.conversation['task_ids'] if tid != self.turn.task_id})
        shadow = copy(self.turn)
        shadow.events = deepcopy(self.turn.events)
        shadow.conversation = deepcopy(self.turn.conversation)
        staging = TaskStore(':memory:')
        try:
            staging.db.executemany('INSERT INTO tasks VALUES (?, ?)', [(tid, json.dumps(state)) for tid, state in snapshots.items()])
            staging.db.commit()
            shadow.store = staging
            result = shadow.finish(answer, validate_only=not commit)
            if any(self.turn.store.get(tid) != state for tid, state in snapshots.items()):
                raise InvalidChange('task changed during delivery; prepare again')
            if commit:
                self.turn.store._save(staging.get(self.turn.task_id))
                self.turn.final = result
                self.turn.conservative_recommendation_message = shadow.conservative_recommendation_message
                self.turn.events = shadow.events
            return result
        finally:
            staging.close()

    def review_payload(self, items, repair=False):
        self.preflight(items, repair)
        rows=[]
        for item in items:
            # Structural failures do not need a model review.
            try:
                self.reason(item)
            except InvalidChange:
                continue
            if not item.reasons:
                continue
            record=self.bundle['mapping'][item.item]
            rows.append({'item':item.item,'reasons':[{'text':r.text,'sources':[
                resolve_reference(self.turn,record['refs'][key],record['pid']) for key in r.facts]}
                for r in item.reasons]})
        return {'user_request':self.turn.text,'requirements':self.turn.requirements(self.bundle['scope']),
                'items':rows}

    def submit(self, items, repair=False, semantic_issues=None, facts_only=False):
        payload = [i.model_dump() for i in items]
        if self.completed:
            if payload == self.submitted:
                return deepcopy(self.completed)
            raise InvalidChange('recommendation already delivered')
        # Invalid selection must never consume semantic review or lock in a draft.
        keys = self.selection(items, repair)
        try:
            self.preflight(items, repair)
        except InvalidChange as exc:
            return {'status': 'selection_needs_revision', 'error': str(exc),
                    'instruction': 'Correct selection/order using prepared items and resubmit; prepare again only for new products or stale evidence. All submitted items are main recommendations.'}
        if not repair:
            self.order = keys
            self.pending = set(keys)
        self.reason_rounds += 1
        errors = []
        for item in items:
            try:
                if semantic_issues and item.item in semantic_issues:
                    raise InvalidChange('reason semantic support: ' + semantic_issues[item.item])
                self.accepted[item.item] = self.reason(item)
                self.pending.discard(item.item)
            except InvalidChange as exc:
                errors.append({'item':item.item,'field':'reasons','error':str(exc)})
        if self.pending and (facts_only or self.reason_rounds >= 2):
            # Only optional rejected prose is removed. Selection/evidence/quantity
            # must still pass the exact same final verifier below.
            dropped = sorted(self.pending)
            for key in dropped:
                self.accepted[key] = {'item': key, 'reasons': []}
            self.pending.clear()
            self.turn.response_draft = None
            event = {'event': 'recommendation_reason_fallback', 'items': dropped,
                     'rounds': self.reason_rounds,
                     'cause': 'review_budget' if facts_only else 'repair_limit'}
            self.turn.events.append(event)
            if self.emit:
                self.emit(event)
        if self.pending:
            return {'status':'repair_required','errors':errors,'pending':sorted(self.pending),
                    'accepted':list(self.accepted),'instruction':'Repair only pending item reasons once; accepted items remain. Any still-rejected optional reasons will be omitted; verified product facts remain.'}
        try:
            result = self.stage(self.answer(), commit=True)
        except InvalidChange as exc:
            return {'status':'selection_needs_revision','error':str(exc),
                    'instruction':'prepare_recommendation again to revise selection/order or evidence; scope remains authoritative.'}
        if all(value.get('fact_selections') is not None and not value.get('reasons') for value in self.accepted.values()):
            self.turn.response_draft = None
            event = {'event': 'source_bound_recommendation', 'product_ids': result['product_ids'],
                     'facts': sum(len(value['fact_selections']) for value in self.accepted.values())}
            self.turn.events.append(event)
            if self.emit:
                self.emit(event)
        self.completed = deepcopy(result)
        self.submitted = payload
        self.turn.events.append({'event':'prepared_recommendation_committed','scope':self.bundle['scope'],'product_ids':result['product_ids']})
        return result
