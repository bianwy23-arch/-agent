"""Pure B1 state invariants. No catalog, model, database or eval access."""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5


def stable_id(task_id, key):
    return str(uuid5(NAMESPACE_URL, task_id + ':' + key))


def empty_decision():
    return {'phase': 'exploration', 'focus_ids': [], 'shortlist_ids': [],
            'selection': None, 'unresolved_tradeoffs': [], 'events': []}


def derive_phase(decision):
    selection = decision.get('selection')
    if selection and selection['commitment'] == 'confirmed':
        decision['phase'] = 'selected' if selection['validity'] == 'valid' else 'needs_confirmation'
    elif decision.get('unresolved_tradeoffs'):
        decision['phase'] = 'needs_confirmation'
    elif len(decision['focus_ids']) > 1:
        decision['phase'] = 'comparison'
    else:
        decision['phase'] = 'exploration'


def effective_requirements(state, scope):
    result = deepcopy(state['requirements'])
    if scope == 'hypothetical':
        if not state['exploration']:
            raise ValueError('no temporary exploration')
        result.update(state['exploration']['overrides'])
    elif scope != 'formal':
        raise ValueError('unknown scope')
    return result


def fingerprint(requirements):
    return sha256(json.dumps(requirements, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_relations(state, *, prune=False):
    active = {r['id'] for r in state['requirements'].values()
              if r['strength'] == 'soft' and r['status'] == 'active' and 'id' in r}
    graph = {pid: [] for pid in active}
    for rel in state['preference_relations'].values():
        if not rel['active']:
            continue
        a, b = rel['higher_preference_id'], rel['lower_preference_id']
        if a not in active or b not in active:
            if prune:
                rel['active'] = False
                continue
            raise ValueError('relation must reference active preferences in this task')
        if a == b:
            raise ValueError('preference relation self-cycle')
        graph[a].append(b)
    done, visiting = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError('preference relation cycle')
        if node in done:
            return
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        done.add(node)
    for node in graph:
        visit(node)


def prepare_requirement(task_id, key, value, source):
    from .contracts import SoftPreference
    from pydantic import ValidationError
    value = deepcopy(value)
    if value['strength'] == 'soft':
        if 'expression' not in value:
            old = value.pop('value')
            # Legacy text has no machine-authorized direction, including 'light'.
            value.update(field=key, expression=None if value['status'] == 'no_preference' else {
                'kind': 'qualitative', 'target': None, 'text': old if isinstance(old, str) else json.dumps(old, ensure_ascii=False)})
        try:
            value = SoftPreference.model_validate(value).model_dump(mode='json')
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        value['id'] = stable_id(task_id, 'preference:' + key)
    value['source'] = deepcopy(source)
    return value


def migrate_task(state, validate_requirement):
    """Validate legacy records before adding defaults; never infer user choices."""
    required = {'id','category','revision','requirements_version','requirements','excluded','candidates',
                'turns','history','receipts','pending','exploration','decision','displays'}
    if not isinstance(state, dict) or not required <= state.keys():
        raise ValueError('malformed legacy task')
    if state.get('schema_version', 1) != 1:
        raise ValueError('unsupported migration source')
    for name in ['requirements','excluded','candidates','turns','receipts','pending','displays','decision']:
        if not isinstance(state[name], dict):
            raise ValueError('malformed legacy field: ' + name)
    if not isinstance(state['history'], list):
        raise ValueError('malformed legacy history')
    for name in ['revision','requirements_version']:
        if type(state[name]) is not int or state[name] < 0:
            raise ValueError('malformed legacy version')
    for pid,candidate in state['candidates'].items():
        if not isinstance(pid,str) or not isinstance(candidate,dict) or not isinstance(candidate.get('facts'),dict):
            raise ValueError('malformed legacy candidate')
    for ids in state['displays'].values():
        if not isinstance(ids,list) or any(not isinstance(pid,str) or pid not in state['candidates'] for pid in ids) or len(set(ids))!=len(ids):
            raise ValueError('malformed legacy display')
    for field in ['focus_ids','shortlist_ids','unresolved_tradeoffs']:
        if not isinstance(state['decision'].get(field),list):
            raise ValueError('malformed legacy decision')
    for event in state['history']:
        if not isinstance(event,dict) or not {'id','turn_id','quote','dependent','writes','undo_of'} <= event.keys():
            raise ValueError('malformed legacy history event')
        if not isinstance(event['writes'],list) or type(event['dependent']) is not bool:
            raise ValueError('malformed legacy history writes')
        for write in event['writes']:
            if not isinstance(write,dict) or not {'target','key','before','after','reverted_by'} <= write.keys() or write['target'] not in {'requirements','excluded'}:
                raise ValueError('malformed legacy history write')
            if write['target']=='requirements':
                for value in [write['before'],write['after']]:
                    if value is not None:
                        validate_requirement(write['key'],{k:v for k,v in value.items() if k!='source'})
    result = deepcopy(state)
    for key, req in state['requirements'].items():
        validate_requirement(key, {k:v for k,v in req.items() if k != 'source'})
        result['requirements'][key] = prepare_requirement(state['id'], key,
            {k:v for k,v in req.items() if k != 'source'}, req.get('source', {'kind':'legacy_unknown'}))
    if state['exploration']:
        for key, req in state['exploration']['overrides'].items():
            validate_requirement(key, {k:v for k,v in req.items() if k!='source'})
            result['exploration']['overrides'][key] = prepare_requirement(state['id'], key,
                {k:v for k,v in req.items() if k!='source'}, req.get('source', {'kind':'legacy_unknown'}))
        result['exploration'].update(id=stable_id(state['id'], 'legacy-exploration'),
                                     decision=empty_decision(), excluded={})
    # v1 has placeholders, not a supported selection-writing contract. Refuse
    # ambiguous populated fields rather than silently erase or invent semantics.
    if state['decision'].get('selection') or state['decision'].get('shortlist_ids'):
        raise ValueError('legacy user decision requires explicit migration review')
    result['decision'].setdefault('events', [])
    result.update(schema_version=2, candidate_evidence_version=0, preference_relations={}, assessments={})
    derive_phase(result['decision'])
    return result
