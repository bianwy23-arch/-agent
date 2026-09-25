"""Transactional state changes. User language interpretation belongs upstream.

This module checks structural invariants and source references; it does not claim
that a quoted user sentence semantically authorizes every model-proposed change.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import sqlite3
from uuid import uuid4
from pathlib import Path

from .decision_state import (empty_decision, derive_phase, stable_id, prepare_requirement,
                             validate_relations, migrate_task, effective_requirements, fingerprint)


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
    if field == 'budget' and value.get('strength') == 'soft':
        raise InvalidChange('budget is a hard ceiling; use a separate price preference')
    if 'expression' in value:
        from .contracts import SoftPreference
        try:
            SoftPreference.model_validate(value)
        except ValueError as exc:
            raise InvalidChange(str(exc)) from exc
        return
    if value.get("field") is None:
        value.pop("field", None)
    attribute = value.get("field", field)
    if not isinstance(attribute, str) or not attribute.strip():
        raise InvalidChange("requirement field must be a nonempty canonical attribute")
    if (field == "budget" or attribute == "budget") and attribute != field:
        raise InvalidChange("budget requires key=budget and field=budget; use field=price for soft price preferences")
    if set(value) - {"field"} != {"status", "strength", "value"}:
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
    elif isinstance(value["value"], dict):
        condition = value["value"]
        if (set(condition) != {"operator", "value", "unit"}
                or condition["operator"] not in {"eq", "gte", "lte", "gt", "lt", "contains", "not_contains"}
                or not isinstance(condition["value"], str) or not condition["value"].strip()
                or (condition["unit"] is not None and not isinstance(condition["unit"], str))):
            raise InvalidChange("malformed attribute predicate")
        # A well-formed but unsupported predicate remains an active requirement.
        # Dataset limitations must not prevent persistence of the user's condition.
    elif not isinstance(value["value"], str) or not value["value"].strip():
        raise InvalidChange("non-budget requirement requires nonempty text")


class TaskStore:
    """One SQLite transaction per group; instances must be closed by callers."""

    def __init__(self, path, *, migrate=False):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        try:
            rows = self.db.execute("SELECT id, state FROM tasks").fetchall()
            versions = [json.loads(raw).get('schema_version', 1) for _, raw in rows]
            if any(type(v) is not int or v not in {1, 2} for v in versions):
                raise InvalidChange('unsupported schema version')
            if 1 in versions:
                if not migrate:
                    raise InvalidChange('schema v1 requires offline migration with all old writers stopped')
                if str(path) != ':memory:':
                    backup = Path(str(path) + '.pre-v2.sqlite')
                    # A failed backup never becomes the durable rollback file.
                    if backup.exists():
                        saved = sqlite3.connect('file:' + str(backup) + '?mode=ro', uri=True)
                        try:
                            if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                                raise InvalidChange('invalid existing migration backup')
                            saved.execute('SELECT id, state FROM tasks LIMIT 1').fetchone()
                        finally:
                            saved.close()
                    else:
                        import os
                        import tempfile
                        fd, temporary = tempfile.mkstemp(prefix=backup.name+'.', dir=backup.parent)
                        os.close(fd)
                        dest = sqlite3.connect(temporary)
                        try:
                            self.db.backup(dest)
                            dest.close()
                            os.replace(temporary, backup)
                        finally:
                            dest.close()
                            if Path(temporary).exists(): Path(temporary).unlink()
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    for tid, raw in self.db.execute('SELECT id, state FROM tasks').fetchall():
                        old = json.loads(raw)
                        if old['id'] != tid:
                            raise InvalidChange('task identity mismatch')
                        if old.get('schema_version', 1) == 1:
                            self._save(migrate_task(old, validate_requirement))
                    self.db.execute('PRAGMA user_version = 2')
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
            self.db.close()
            raise InvalidChange(str(exc)) from exc

    def close(self):
        self.db.close()

    def create(self, category):
        if not isinstance(category, str) or not category:
            raise InvalidChange("category required")
        task_id = str(uuid4())
        state = {"id": task_id, "category": category, "revision": 0,
                 "schema_version": 2, "candidate_evidence_version": 0, "preference_relations": {},
                 "requirements_version": 0, "requirements": {}, "excluded": {},
                 "candidates": {}, "turns": {}, "history": [], "receipts": {},
                 "pending": {}, "exploration": None,
                 "assessments": {},
                 "decision": {"phase": "exploration", "focus_ids": [],
                              "shortlist_ids": [], "unresolved_tradeoffs": [],
                              "selection": None, "events": []}, "displays": {},
                 "hypotheses": {}, "rejected_directions": [], "question_history": [],
                 "progress": {"fingerprint": None, "repeats": 0}}
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, json.dumps(state)))
        return task_id

    def add_hypotheses(self, task_id, items):
        with self.db:
            state = self.get(task_id)
            for item in items:
                state.setdefault('hypotheses', {})[item['id']] = deepcopy(item)
            self._save(state)

    def record_question(self, task_id, turn_id, question, gap, message):
        from .action_policy import question_basis
        with self.db:
            state = self.get(task_id)
            record = {"question_key": gap["id"], "turn_id": turn_id, "fields": gap.get("fields", []),
                      "action": question["action"], "expected_information": question["expected_information"],
                      "message": message, "basis": question_basis(state, gap),
                      "requirements_version": state["requirements_version"],
                      "evidence_version": state["candidate_evidence_version"], "response": None}
            if any(q["question_key"] == record["question_key"] and q.get("basis") == record["basis"]
                   for q in state["question_history"]):
                raise InvalidChange("question already asked against this evidence; use conditional delivery or disclose limits")
            state["question_history"].append(record)
            self._save(state)

    def record_question_response(self, task_id, turn_id, text):
        # Capture the actual next user message; never label it as a resolved answer
        # merely because a response arrived. Requirement changes still use groups.
        with self.db:
            state = self.get(task_id)
            for question in state["question_history"]:
                if question.get("response") is None and question["turn_id"] != turn_id:
                    question["response"] = {"turn_id": turn_id, "text": text, "status": "received_unclassified"}
            self._save(state)

    def get(self, task_id):
        row = self.db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise InvalidChange("unknown task")
        state = json.loads(row[0])
        if state.get('schema_version') != 2:
            raise InvalidChange('unsupported schema version')
        state.setdefault('hypotheses', {})
        state.setdefault('scenarios', {})
        state.setdefault('display_scopes', {})
        state.setdefault('action_history', [])
        state.setdefault('rejected_directions', [])
        state.setdefault('question_history', [])
        state.setdefault('progress', {'fingerprint': None, 'repeats': 0})
        return state

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
            candidate.pop("assessment", None)
            candidate["user_excluded"] = candidate_id in state["excluded"]
        # Facts survive; conclusions must be evaluated against current conditions.
        state["decision"]["needs_reassessment"] = True
        for decision in [state['decision']] + ([state['exploration']['decision']] if state['exploration'] else []):
            selection = decision.get('selection')
            if selection and requirement_changed:
                selection['validity'] = 'unchecked'
                selection['checked_requirements_version'] = None
                selection['effective_requirements_fingerprint'] = None
            derive_phase(decision)

    @staticmethod
    def _bucket(state, target):
        if target.startswith('temporary_'):
            if not state['exploration']:
                raise InvalidChange('temporary state no longer exists')
            return state['exploration'][target[len('temporary_'):]]
        return state[target]

    @classmethod
    def _write(cls, state, target, key, value):
        bucket = cls._bucket(state, target)
        if value is None and key != 'selection':
            bucket.pop(key, None)
        else:
            bucket[key] = deepcopy(value)

    @staticmethod
    def _soft_on_field(state, field):
        for req in state['requirements'].values():
            if req.get('strength') == 'soft' and req.get('field') == field:
                return req
        return None

    def apply_group(self, task_id, turn_id, group_id, operations, quote, *, dependent=False, scope='formal'):
        """Resolve first, validate the complete proposed group, then commit its diff."""
        from .contracts import DecisionInput, PreferenceRelation
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            key = self._key(turn_id, group_id)
            payload = {'operations': operations, 'quote': quote, 'dependent': dependent}
            if scope != 'formal':
                payload['scope'] = scope
            if key in state['receipts']:
                if state['receipts'][key] != payload:
                    raise InvalidChange('applied group ID reused with different payload')
                return 'already_applied'
            if not isinstance(operations, list) or not operations or type(dependent) is not bool:
                raise InvalidChange('nonempty operations and boolean dependent required')
            if scope not in {'formal', 'hypothetical'}:
                raise InvalidChange('unknown scope')
            if scope == 'hypothetical':
                if not state['exploration']:
                    raise InvalidChange('no temporary exploration')
                if state['exploration']['base_requirements_version'] != state['requirements_version']:
                    raise InvalidChange('temporary exploration is stale')
            work = deepcopy(state)
            targets, restored, selected, excluded = set(), set(), set(), set()
            normalized = []
            # Bind every position using the same pre-application display snapshot.
            for op in operations:
                if not isinstance(op, dict) or set(op) != {'target', 'key', 'value'}:
                    raise InvalidChange('operation requires only target, key, value')
                target, name, value = op['target'], op['key'], deepcopy(op['value'])
                if not isinstance(name, str) or not name or (target, name) in targets:
                    raise InvalidChange('invalid or conflicting writes')
                targets.add((target, name))
                if scope == 'hypothetical' and target != 'decision':
                    raise InvalidChange('temporary apply accepts decision operations; use explore for conditions')
                if target == 'decision':
                    try:
                        value = DecisionInput.model_validate(value).model_dump()
                    except ValueError as exc:
                        raise InvalidChange(str(exc)) from exc
                    display = value['display_id']
                    if value['positions']:
                        if value['product_ids'] or not display:
                            raise InvalidChange('positions require one display and no product_ids')
                        value['product_ids'] = [self.resolve(task_id, display, n) for n in value['positions']]
                    ids = value['product_ids']
                    if len(set(ids)) != len(ids) or any(pid not in state['candidates'] for pid in ids):
                        raise InvalidChange('decision requires unique known products')
                    if display and (display not in state['displays'] or any(pid not in state['displays'][display] for pid in ids)):
                        raise InvalidChange('product not in referenced task display')
                    if name in {'select_tentative', 'select_confirmed'}:
                        if len(ids) != 1 or not display:
                            raise InvalidChange('selection requires one product and actual display')
                        selected.update(ids)
                    if name in {'focus_set','shortlist_add'}: selected.update(ids)
                    if name == 'exclude': excluded.update(ids)
                    if name == 'restore': restored.update(ids)
                elif target == 'excluded':
                    (excluded if value is True else restored).add(name)
                normalized.append((target, name, value))
            if selected & excluded or restored & excluded:
                raise InvalidChange('ambiguous exclude/restore/select group; clarify user intent')
            source = {'kind': 'explicit', 'turn_id': turn_id, 'quote': quote}
            decision_target = 'decision' if scope == 'formal' else 'temporary_decision'
            exclusion_target = 'excluded' if scope == 'formal' else 'temporary_excluded'
            decision = self._bucket(work, decision_target)
            changed_targets = {'requirements', 'excluded', 'preference_relations', 'decision'}
            if scope == 'hypothetical': changed_targets = {decision_target, exclusion_target}
            for target, name, value in normalized:
                if target == 'requirements':
                    if value is not None:
                        validate_requirement(name, value)
                        # A soft proposal must never overwrite a hard requirement.
                        prior = work['requirements'].get(name)
                        if prior and 'field' in prior and value.get('field',name) != prior['field']:
                            raise InvalidChange('cannot retarget a stable preference ID to another field')
                        if prior and prior['strength'] != value['strength']:
                            raise InvalidChange(
                                f"Requirement key {name!r} already stores a {prior['strength']} goal. "
                                f"Keep it; write the new goal with key={value['strength'] + ':' + prior.get('field', name)!r} "
                                f"and explicit field={prior.get('field', name)!r}. "
                                "To replace the old goal, delete its key explicitly in a separate group. "
                                "Never encode the attribute by inventing suffixes such as _max.")
                        try:
                            value = prepare_requirement(task_id, name, value, source)
                        except ValueError as exc:
                            raise InvalidChange(str(exc)) from exc
                        if value['strength'] == 'soft':
                            if any(k != name and r['strength']=='soft' and r.get('field')==value['field'] for k,r in work['requirements'].items()):
                                raise InvalidChange('one authoritative soft preference per field')
                    self._write(work, target, name, value)
                    if name == 'budget' and value and value != state['requirements'].get(name):
                        work['rejected_directions'] = [d for d in work.get('rejected_directions',[]) if d != 'raise_budget']
                    if value and value.get('strength') == 'soft':
                        field = value.get('field')
                        for hyp in work.get('hypotheses', {}).values():
                            if hyp.get('status') == 'proposed' and hyp.get('proposed_soft_expression', {}).get('field') == field:
                                if value.get('status') == 'no_preference' or (value.get('source') or {}).get('kind') == 'explicit':
                                    hyp['status'] = 'invalidated'
                                    changed_targets.add('hypotheses')
                elif target == 'relations':
                    if value is None:
                        if name not in work['preference_relations']:
                            raise InvalidChange('unknown relation')
                        work['preference_relations'][name]['active'] = False
                    else:
                        try:
                            value = PreferenceRelation.model_validate(value).model_dump()
                        except ValueError as exc:
                            raise InvalidChange(str(exc)) from exc
                        # Canonical keys may refer to preferences created earlier
                        # in this same atomic group. Persist only task-owned IDs.
                        for reference in ('higher_preference_id', 'lower_preference_id'):
                            supplied = value[reference]
                            matches = [r['id'] for key, r in work['requirements'].items()
                                       if r.get('strength') == 'soft' and r.get('status') == 'active'
                                       and supplied in {key, r.get('id')}]
                            if len(matches) == 1:
                                value[reference] = matches[0]
                        work['preference_relations'][name] = {**value, 'id': stable_id(task_id, 'relation:'+name), 'source': source, 'active': True}
                elif target == 'excluded':
                    if name not in state['candidates'] or (value is not True and value is not None):
                        raise InvalidChange('exclusion requires known product and true or null')
                    self._write(work, 'excluded', name, value)
                elif target == 'decision':
                    ids = value['product_ids']
                    banned = set(state['excluded']) | set(self._bucket(work, exclusion_target))
                    if name in {'shortlist_add', 'focus_set', 'select_tentative', 'select_confirmed'} and set(ids) & (banned-restored):
                        raise InvalidChange('restore excluded product before retaining or selecting it')
                    if name in {'shortlist_add','shortlist_remove'}:
                        if not ids: raise InvalidChange('shortlist action requires products')
                        if name == 'shortlist_add':
                            decision['shortlist_ids'] = list(dict.fromkeys(decision['shortlist_ids'] + ids))
                        else:
                            decision['shortlist_ids'] = [pid for pid in decision['shortlist_ids'] if pid not in ids]
                    elif name == 'focus_set': decision['focus_ids'] = ids
                    elif name in {'select_tentative','select_confirmed'}:
                        decision['selection'] = {'product_id': ids[0], 'commitment': 'confirmed' if name=='select_confirmed' else 'tentative',
                            'validity':'unchecked', 'source':source, 'scope':scope, 'proposal_id':None,
                            'display_id':value['display_id'], 'checked_requirements_version':None,
                            'effective_requirements_fingerprint':None}
                    elif name == 'selection_clear':
                        if ids: raise InvalidChange('selection_clear has no product arguments')
                        decision['selection'] = None
                    elif name in {'exclude','restore'}:
                        if not ids: raise InvalidChange('exclude/restore requires products')
                        for pid in ids: self._write(work, exclusion_target, pid, True if name=='exclude' else None)
                    else: raise InvalidChange('unsupported decision action; use select_confirmed/select_tentative/selection_clear/shortlist_add/shortlist_remove/focus_set/exclude/restore; selection is not an action')
                elif target == 'scenarios':
                    from .contracts import ScenarioValue
                    try:
                        scene = ScenarioValue.model_validate(value).model_dump()
                    except ValueError as exc:
                        raise InvalidChange(str(exc)) from exc
                    if scene['text'] not in quote:
                        raise InvalidChange('scenario text must quote its user source')
                    prior = work.setdefault('scenarios', {}).get(name)
                    version = (prior or {}).get('version', 0) + (not prior or prior['active'] != scene['active'])
                    work['scenarios'][name] = {**scene, 'id': stable_id(task_id, 'scenario:'+name),
                        'version': version, 'source': source}
                    changed_targets.add('scenarios')
                    for hid, hyp in work.get('hypotheses', {}).items():
                        refs = hyp.get('scenario_versions', {})
                        if name in refs and (not scene['active'] or refs[name] != version):
                            hyp['status'] = 'invalidated'
                            hyp['invalidated_by'] = 'scenarios:' + name
                            changed_targets.add('hypotheses')
                            for k, req in list(work['requirements'].items()):
                                if req.get('source', {}).get('hypothesis_id') == hid:
                                    del work['requirements'][k]
                                    changed_targets.add('requirements')
                    # This small frozen rule table is a source-linked proposal,
                    # not an inferred explicit user requirement.
                    from .action_policy import RULES
                    for rule in RULES:
                        if work['category'] not in {'headphones','bluetooth_speakers'} or name != rule.get('scenario') or not scene['active']:
                            continue
                        hid = stable_id(task_id, 'rule:'+rule['id']+':'+str(version))
                        current = self._soft_on_field(work, rule['field'])
                        if hid in work['hypotheses'] or current or any(w['target']=='hypotheses' and w['key']==hid and w.get('reverted_by') for e in work['history'] for w in e['writes']):
                            continue
                        work['hypotheses'][hid] = {'id': hid, 'status': 'proposed', 'origin':'rule',
                            'rule_id':rule['id'], 'rule_version':rule['version'],
                            'scenario_ids':[work['scenarios'][name]['id']], 'scenario_versions':{name:version},
                            'source_refs':[source], 'proposed_soft_expression':{'field':rule['field'], **deepcopy(rule['expression'])},
                            'reason':rule['reason'], 'limitations':rule['limitations']}
                        changed_targets.add('hypotheses')
                elif target == 'hypotheses':
                    if isinstance(value, dict):
                        from .contracts import HypothesisProposal
                        try:
                            proposal = HypothesisProposal.model_validate(value).model_dump()
                        except ValueError as exc:
                            raise InvalidChange(str(exc)) from exc
                        scenes = work.get('scenarios', {})
                        if any(k not in scenes or not scenes[k]['active'] for k in proposal['scenario_keys']):
                            raise InvalidChange('hypothesis requires active scenario sources')
                        if self._soft_on_field(work, proposal['field']):
                            raise InvalidChange('hypothesis cannot override explicit preference or no_preference')
                        if name in work['hypotheses']:
                            raise InvalidChange('cannot overwrite a hypothesis; use its confirmation/rejection action')
                        refs = {k:scenes[k]['version'] for k in proposal['scenario_keys']}
                        if any(h.get('scenario_versions') == refs and h['proposed_soft_expression']['field'] == proposal['field']
                               for h in work['hypotheses'].values()):
                            raise InvalidChange('same scenario/field/source already proposed or rejected')
                        work['hypotheses'][name] = {'id':name, 'status':'proposed','origin':'model',
                            'scenario_ids':[scenes[k]['id'] for k in refs], 'scenario_versions':refs,
                            'source_refs':[source], 'proposed_soft_expression':{'field':proposal['field'], **proposal['expression']},
                            'reason':proposal['reason'], 'limitations':proposal['limitations']}
                        changed_targets.add('hypotheses')
                        continue
                    item = work.setdefault('hypotheses', {}).get(name)
                    if item is None:
                        raise InvalidChange('unknown hypothesis')
                    if value is True:
                        if item['status'] not in {'proposed'}:
                            raise InvalidChange('only a proposed hypothesis can be confirmed')
                        expression = deepcopy(item['proposed_soft_expression'])
                        field = expression.pop('field')
                        current = self._soft_on_field(work, field)
                        if current and current.get('status') == 'no_preference':
                            raise InvalidChange('confirmed hypothesis cannot override no_preference')
                        key = 'soft:' + field if field in work['requirements'] and work['requirements'][field]['strength']=='hard' else field
                        pref = {'field': field, 'status': 'active', 'strength': 'soft', 'expression': expression}
                        pref = prepare_requirement(task_id, key, pref, {'kind': 'user_confirmed', 'turn_id': turn_id, 'quote': quote, 'hypothesis_id': name})
                        work['requirements'][key] = pref
                        item['status'] = 'confirmed'
                        changed_targets.add('requirements')
                    elif value is False:
                        for pref_key, pref in list(work['requirements'].items()):
                            if (pref.get('source') or {}).get('hypothesis_id') == name:
                                del work['requirements'][pref_key]
                                changed_targets.add('requirements')
                        item['status'] = 'rejected'
                        work.setdefault('rejected_directions', [])
                        if item.get('rule_id') and item['rule_id'] not in work['rejected_directions']:
                            work['rejected_directions'].append(item['rule_id'])
                    else:
                        raise InvalidChange('hypothesis value must be true/false')
                    changed_targets.add('hypotheses')
                else: raise InvalidChange('unsupported target')
            side_effects = {}
            for target, name, value in normalized:
                if target == 'scenarios':
                    for hid in set(state.get('hypotheses', {})) | set(work.get('hypotheses', {})):
                        hyp = work.get('hypotheses', {}).get(hid) or state['hypotheses'][hid]
                        if name in hyp.get('scenario_versions', {}):
                            side_effects[('hypotheses',hid)] = ['scenarios:'+name]
                            for pk in set(state['requirements']) | set(work['requirements']):
                                if any(p.get('source',{}).get('hypothesis_id') == hid for p in
                                       [state['requirements'].get(pk,{}),work['requirements'].get(pk,{})]):
                                    side_effects[('requirements',pk)] = ['scenarios:'+name]
            for target, name, value in normalized:
                if target == 'hypotheses':
                    for pref_key in set(state['requirements']) | set(work['requirements']):
                        before_pref = state['requirements'].get(pref_key, {})
                        after_pref = work['requirements'].get(pref_key, {})
                        if any((p.get('source') or {}).get('hypothesis_id') == name for p in (before_pref, after_pref)):
                            side_effects[('requirements', pref_key)] = ['hypotheses:' + name]
            # Exclusion cleanup is part of its group, including a current temporary view.
            cleanup = [(decision_target, decision, set(work['excluded']) | set(self._bucket(work, exclusion_target)))]
            if scope == 'formal' and work['exploration']:
                cleanup.append(('temporary_decision',work['exploration']['decision'],set(work['excluded']) | set(work['exploration']['excluded'])))
                changed_targets.add('temporary_decision')
            for cleanup_target, current_decision, banned in cleanup:
                for field in ['focus_ids','shortlist_ids']:
                    removed = set(current_decision[field]) & banned
                    if removed:
                        side_effects[(cleanup_target,field)] = [exclusion_target+':'+pid for pid in sorted(removed)]
                    current_decision[field] = [pid for pid in current_decision[field] if pid not in banned]
                if current_decision['selection'] and current_decision['selection']['product_id'] in banned:
                    side_effects[(cleanup_target,'selection')] = [exclusion_target+':'+current_decision['selection']['product_id']]
                    current_decision['selection']['validity'] = 'needs_review'
            try:
                # Removing a preference invalidates only its dependent relations;
                # explicitly proposed dangling relations are rejected, not hidden.
                new_relations = {name for target,name,value in normalized if target=='relations' and value is not None}
                active = {r.get('id') for r in work['requirements'].values() if r['strength']=='soft' and r['status']=='active'}
                for name in new_relations:
                    rel = work['preference_relations'][name]
                    if not {rel['higher_preference_id'], rel['lower_preference_id']} <= active:
                        raise ValueError('relation must reference active preferences in this task')
                before_prune = deepcopy(work['preference_relations'])
                validate_relations(work, prune=True)
                for rel_key, rel in work['preference_relations'].items():
                    if before_prune[rel_key]['active'] and not rel['active']:
                        refs = {rel['higher_preference_id'],rel['lower_preference_id']}
                        side_effects[('preference_relations',rel_key)] = ['requirements:'+k for k,r in state['requirements'].items() if r.get('id') in refs]
            except ValueError as exc: raise InvalidChange(str(exc)) from exc
            writes = []
            for target in sorted(changed_targets):
                before, after = self._bucket(state,target), self._bucket(work,target)
                for name in sorted(before.keys() | after.keys()):
                    if before.get(name) != after.get(name):
                        writes.append({'target':target,'key':name,'before':deepcopy(before.get(name)),
                                       'after':deepcopy(after.get(name)),'reverted_by':None,
                                       'side_effect_of':side_effects.get((target,name),[]),
                                       'ephemeral_side_effect':scope=='formal' and target.startswith('temporary_'),
                                       'combined_explicit_write': (target in {'decision','temporary_decision'} and bool(side_effects.get((target,name))) and
                                          any(t=='decision' and k in ({'shortlist_ids':{'shortlist_add','shortlist_remove'},'focus_ids':{'focus_set'},'selection':{'select_confirmed','select_tentative','selection_clear'}}.get(name,set())) for t,k,v in normalized))})
            work['history'].append({'id':key,'turn_id':turn_id,'quote':quote,'dependent':dependent,
                'writes':writes,'undo_of':[], 'exploration_id': state['exploration']['id'] if any(w['target'].startswith('temporary_') for w in writes) else None})
            work['receipts'][key] = deepcopy(payload)
            work['pending'].pop(key, None)
            resolved = {w['key'] for w in writes if w['target']=='requirements'}
            for pending_key,pending in list(work['pending'].items()):
                if set(pending['fields']) <= resolved: del work['pending'][pending_key]
            if any(w['target'] in {'decision','temporary_decision','excluded','temporary_excluded'} for w in writes):
                decision['events'].append({'id':key,'turn_id':turn_id,'source':source,'scope':scope,
                                           'operations':deepcopy(operations)})
            self._refresh(work, any(w['target'] in {'requirements','preference_relations'} for w in writes))
            from .decision_effects import receipt
            effect = receipt(state, work, normalized, turn_id, group_id, scope)
            if effect:
                work.setdefault('decision_effects', {})[key] = effect
            self._save(work)
        return 'applied'

    def undo(self, task_id, turn_id, group_id, quote, *, field=None, scope="formal"):
        """Undo last applied user turn, or the latest change to one requirement.

        A pending-only previous turn is not skipped to undo an older user turn.
        Partial undo of dependent groups is rejected for upstream clarification.
        """
        with self.db:
            state = self.get(task_id)
            self._source(state, turn_id, quote)
            key = self._key(turn_id, group_id)
            receipt = {"undo_field": field, "quote": quote}
            if scope not in {'formal','hypothetical'}: raise InvalidChange('unknown undo scope')
            if scope == 'hypothetical':
                if not state['exploration']: raise InvalidChange('no temporary exploration')
                receipt['scope'] = scope
            if key in state["receipts"]:
                if state["receipts"][key] != receipt:
                    raise InvalidChange("applied group ID reused")
                return "already_applied"
            # Duplicate interpretation of the same undo intent in this turn
            # must not create a second undo or turn a successful undo into failure.
            prior_undo_ids = {event["id"] for event in state["history"] if event["turn_id"] == turn_id and event["undo_of"]}
            if any(state["receipts"].get(old_key) == receipt for old_key in prior_undo_ids):
                state["receipts"][key] = receipt
                self._save(state)
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
                chosen = [w for w in active if field is None or ((w["target"] == "requirements" and w["key"] == field) or (w["target"] + ':' + w["key"] == field))]
                if scope == 'hypothetical':
                    chosen = [w for w in chosen if w['target'].startswith('temporary_') and not w.get('ephemeral_side_effect')]
                if event.get('exploration_id') and (not state['exploration'] or state['exploration']['id'] != event['exploration_id']):
                    chosen = [w for w in chosen if not w.get('ephemeral_side_effect')]
                if not chosen:
                    continue
                if any(w['target'].startswith('temporary_') for w in chosen) and event.get('exploration_id') and (not state['exploration'] or state['exploration']['id'] != event['exploration_id']):
                    raise InvalidChange('cannot undo a decision from an ended temporary plan')
                # Exclusion cleanup and preference relationship cleanup must undo together.
                if field is not None and any(w['target'] in {'excluded','temporary_excluded','requirements','hypotheses','scenarios'} for w in chosen):
                    refs = {w['target']+':'+w['key'] for w in chosen}
                    extras = [w for w in active if refs & set(w.get('side_effect_of',[])) and
                              not (w.get('ephemeral_side_effect') and (not state['exploration'] or state['exploration']['id'] != event.get('exploration_id')))]
                    if any(w.get('combined_explicit_write') for w in extras):
                        raise InvalidChange('partial undo would split a combined decision and exclusion write')
                    chosen = chosen + [w for w in extras if w not in chosen]
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
                    restored = deepcopy(old['before'])
                    current = self._bucket(state,target).get(name)
                    if old.get('side_effect_of') and target == 'requirements' and current != old['after']:
                        # A later independent explicit preference is not owned by
                        # the hypothesis operation being undone.
                        restored = deepcopy(current)
                    if old.get('side_effect_of') and target in {'decision','temporary_decision'}:
                        if name in {'shortlist_ids','focus_ids'}:
                            removed = set(old['before']) - set(old['after'])
                            added = set(old['after']) - set(old['before'])
                            restored = [pid for pid in current if pid not in added]
                            for pos,pid in enumerate(old['before']):
                                if pid in removed and pid not in restored:
                                    restored.insert(min(pos,len(restored)),pid)
                        elif name=='selection':
                            # Keep a later explicit selection or clearing action.
                            if current is None or any(current.get(k)!=old['after'].get(k) for k in ['product_id','commitment','source']):
                                restored = deepcopy(current)
                    writes.append({"target": target, "key": name,
                                   "before": deepcopy(self._bucket(state, target).get(name)),
                                   "after": restored, "reverted_by": None,
                                   "side_effect_of":deepcopy(old.get('side_effect_of',[])),
                                   "ephemeral_side_effect":old.get("ephemeral_side_effect",False)})
                    self._write(state, target, name, restored)
                    old["reverted_by"] = key
            state["history"].append({"id": key, "turn_id": turn_id, "quote": quote,
                                     "dependent": any(e["dependent"] for e, _ in selected),
                                     "writes": writes, "undo_of": [e["id"] for e, _ in selected],
                                     "exploration_id": state['exploration']['id'] if any(w['target'].startswith('temporary_') for w in writes) else None})
            state["receipts"][key] = receipt
            try:
                before_relations = deepcopy(state['preference_relations'])
                validate_relations(state, prune=True)
                for name, relation in state['preference_relations'].items():
                    if relation != before_relations.get(name):
                        writes.append({'target':'preference_relations','key':name,'before':before_relations.get(name),'after':deepcopy(relation),'reverted_by':None})
            except ValueError as exc: raise InvalidChange(str(exc)) from exc
            # A partial undo cannot revive a derived preference while leaving
            # its user-rejected or inactive scenario source in place.
            for req in state['requirements'].values():
                source = req.get('source',{})
                if req.get('status')=='active' and source.get('kind')=='user_confirmed' and source.get('hypothesis_id'):
                    hyp = state.get('hypotheses',{}).get(source['hypothesis_id'],{})
                    valid = hyp.get('status')=='confirmed' and all(
                        state.get('scenarios',{}).get(k,{}).get('active') and
                        state['scenarios'][k]['version']==v for k,v in hyp.get('scenario_versions',{}).items())
                    if not valid:
                        raise InvalidChange('partial undo would restore a preference with an inactive hypothesis source; undo the source scenario group together')
            # Restored historical soft values remain qualitative, never guessed.
            for name, req in list(state['requirements'].items()):
                if req['strength']=='soft' and 'expression' not in req:
                    state['requirements'][name] = prepare_requirement(task_id,name,{k:v for k,v in req.items() if k!='source'},req.get('source',{}))
            for target in ['decision'] + (['temporary_decision'] if state['exploration'] else []):
                decision = self._bucket(state,target)
                if any(w['target']==target and w['key']=='selection' for w in writes) and decision.get('selection'):
                    decision['selection']['validity'] = 'unchecked'
                    decision['selection']['checked_requirements_version'] = None
                    decision['selection']['effective_requirements_fingerprint'] = None
            state['decision']['events'].append({'id':key,'turn_id':turn_id,'source':{'quote':quote},'undo_of':[e['id'] for e,_ in selected]})
            self._refresh(state, any(w['target'] in {'requirements','preference_relations'} for w in writes))
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
            overrides = {field: (prepare_requirement(task_id,field,value,{'kind':'explicit','turn_id':turn_id,'quote':quote})
                         if value['strength']=='soft' else value) for field,value in overrides.items()}
            existing = state['exploration']
            if (existing and existing['turn_id'] == turn_id and
                    existing['base_requirements_version'] == state['requirements_version']):
                # Independent groups and repair calls in one turn form one overlay.
                # Preserve already-applied fields and their original provenance.
                def meaning(value):
                    return {k: v for k, v in value.items() if k not in {'source', 'id'}}
                for field, value in overrides.items():
                    if field in existing['overrides'] and meaning(existing['overrides'][field]) != meaning(value):
                        raise InvalidChange('cannot change an established exploration on retry; clarify conflicting temporary conditions')
                additions = {k: deepcopy(v) for k, v in overrides.items() if k not in existing['overrides']}
                if additions:
                    existing['overrides'].update(additions)
                    self._save(state)
                return
            state["exploration"] = {"id":str(uuid4()), "decision":empty_decision(), "excluded":{},
                                    "turn_id": turn_id, "quote": quote,
                                    "overrides": deepcopy(overrides),
                                    "base_requirements_version": state["requirements_version"]}
            self._save(state)

    def end_exploration(self, task_id):
        with self.db:
            state = self.get(task_id)
            state["exploration"] = None
            self._save(state)

    def remember(self, task_id, product_id, facts, *, normalized=None):
        """Trusted catalog adapter only, never direct model-supplied facts."""
        if not isinstance(product_id, str) or not product_id or not isinstance(facts, dict):
            raise InvalidChange("candidate ID and facts required")
        with self.db:
            state = self.get(task_id)
            candidate = state["candidates"].setdefault(product_id, {"facts": {}})
            if normalized is None:
                # Older callers save raw facts. Normalize only those saved sources,
                # never silently read additional catalog evidence.
                from .qualification import normalize
                product = {'attributes': {k: v for k, v in facts.items()
                           if k != 'price' and isinstance(v, dict) and v.get('evidence')}}
                normalized = {k: normalize(product, k) for k in product['attributes']}
            changed = (any(candidate['facts'].get(k) != v for k,v in facts.items()) or
                       any(candidate.get('normalized', {}).get(k) != v for k,v in normalized.items()))
            if changed:
                state['candidate_evidence_version'] += 1
                for decision in [state['decision']] + ([state['exploration']['decision']] if state['exploration'] else []):
                    if decision.get('selection') and decision['selection']['product_id'] == product_id:
                        decision['selection']['validity'] = 'unchecked'
                        derive_phase(decision)
            candidate["facts"].update(deepcopy(facts))
            candidate.setdefault("normalized", {}).update(deepcopy(normalized))
            candidate["qualification"] = "unknown"
            candidate["user_excluded"] = product_id in state["excluded"]
            self._save(state)

    def save_assessment(self, task_id, assessment_id, assessment):
        """Persist an immutable assessment bound to its task and versions."""
        if not isinstance(assessment_id, str) or not assessment_id:
            raise InvalidChange("assessment ID required")
        with self.db:
            state = self.get(task_id)
            value = deepcopy(assessment)
            if value.get("task_id") != task_id:
                raise InvalidChange("assessment task mismatch")
            existing = state.setdefault("assessments", {}).get(assessment_id)
            if existing is not None:
                if existing != value:
                    raise InvalidChange("assessment ID is immutable")
                return "already_saved"
            state["assessments"][assessment_id] = value
            self._save(state)
        return "saved"

    def get_assessment(self, task_id, assessment_id):
        state = self.get(task_id)
        try:
            value = deepcopy(state["assessments"][assessment_id])
        except KeyError as exc:
            raise InvalidChange("unknown assessment") from exc
        if value.get("requirements_version") != state["requirements_version"] or value.get("evidence_version") != state["candidate_evidence_version"]:
            raise InvalidChange("assessment is stale; reassess against current requirements and evidence")
        return value

    def assess(self, task_id, product_id, assessment):
        """Persist a trusted formal assessment; facts and hypothetical checks stay separate."""
        with self.db:
            state = self.get(task_id)
            candidate = state["candidates"][product_id]
            value = deepcopy(assessment)
            if product_id in state["excluded"]:
                value["violated"].append("user_excluded")
            candidate["assessment"] = {**value, "requirements_version": state["requirements_version"]}
            candidate["qualification"] = ("violated" if value["violated"] else "unknown" if value["unknown"] or value["conflict"] else "satisfied")
            self._save(state)

    def display(self, task_id, display_id, product_ids, scope="formal"):
        with self.db:
            state = self.get(task_id)
            if not display_id or not product_ids or len(set(product_ids)) != len(product_ids):
                raise InvalidChange("display requires unique IDs")
            if any(pid not in state["candidates"] for pid in product_ids):
                raise InvalidChange("display contains unknown candidate")
            if display_id in state["displays"] and state["displays"][display_id] != product_ids:
                raise InvalidChange("display ID is immutable")
            if scope not in {'formal','hypothetical'} or scope=='hypothetical' and not state.get('exploration'):
                raise InvalidChange('display requires a current valid scope')
            metadata={'scope':scope,'exploration_id':state['exploration']['id'] if scope=='hypothetical' else None}
            if display_id in state['display_scopes'] and state['display_scopes'][display_id]!=metadata:
                raise InvalidChange('display scope is immutable')
            state['display_scopes'][display_id]=metadata
            state["displays"][display_id] = list(product_ids)
            self._save(state)

    def resolve(self, task_id, display_id, position):
        ids = self.get(task_id)["displays"].get(display_id)
        if ids is None or type(position) is not int or not 1 <= position <= len(ids):
            raise InvalidChange("unknown display or invalid position")
        return ids[position - 1]

    def revalidate_decisions(self, task_id, catalog):
        """Trusted service entry; never accepts model-authored qualification."""
        with self.db:
            state = self.get(task_id)
            scopes = [('formal',state['decision'])]
            if state['exploration']: scopes.append(('hypothetical',state['exploration']['decision']))
            for scope, decision in scopes:
                selection = decision.get('selection')
                requirements = effective_requirements(state,scope)
                ids = set(decision['shortlist_ids']) | set(decision['focus_ids'])
                if selection: ids.add(selection['product_id'])
                results = {}
                for pid in sorted(ids):
                    q = catalog.qualification(pid,requirements)
                    if pid in state['excluded'] or (scope=='hypothetical' and pid in state['exploration']['excluded']):
                        q['violated'].append('user_excluded')
                    results[pid] = q
                    if scope=='formal':
                        state['candidates'][pid]['assessment'] = {**deepcopy(q),'requirements_version':state['requirements_version']}
                        state['candidates'][pid]['qualification'] = 'violated' if q['violated'] else 'unknown' if q['unknown'] or q['conflict'] else 'satisfied'
                if scope=='hypothetical': state['exploration']['qualifications'] = results
                if not selection:
                    derive_phase(decision)
                    continue
                result = results[selection['product_id']]
                banned = selection['product_id'] in state['excluded'] or (scope=='hypothetical' and selection['product_id'] in state['exploration']['excluded'])
                stale_scope = scope=='hypothetical' and state['exploration']['base_requirements_version'] != state['requirements_version']
                selection['validity'] = 'needs_review' if banned or stale_scope or result['violated'] or result['unknown'] or result['conflict'] else 'valid'
                selection['checked_requirements_version'] = state['requirements_version']
                selection['effective_requirements_fingerprint'] = fingerprint(requirements)
                derive_phase(decision)
            if any('decision' in p['fields'] for p in state['pending'].values()):
                state['decision']['phase'] = 'needs_confirmation'
            self._save(state)
