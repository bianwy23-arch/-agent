"""Observable decision effects. These facts do not infer the user's intent."""
from copy import deepcopy
import json


def bucket(state, scope):
    return (state.get('exploration') or {}).get('decision', {}) if scope == 'hypothetical' else state.get('decision', {})


def snapshot(state, scope):
    d = bucket(state, scope)
    return {k: deepcopy(d.get(k)) for k in ('shortlist_ids', 'focus_ids', 'selection')}


def receipt(before, after, normalized, turn_id, group_id, scope):
    operations = []
    bd, ad = bucket(before, scope), bucket(after, scope)
    for target, action, value in normalized:
        if target != 'decision':
            continue
        ids = value['product_ids']
        field = 'shortlist_ids' if action.startswith('shortlist_') else 'focus_ids' if action == 'focus_set' else 'selection' if action.startswith('select') else 'excluded'
        if field == 'excluded':
            b = (before.get('exploration') or {}).get('excluded', {}) if scope == 'hypothetical' else before.get('excluded', {})
            a = (after.get('exploration') or {}).get('excluded', {}) if scope == 'hypothetical' else after.get('excluded', {})
        else:
            b, a = bd.get(field), ad.get(field)
        added = [p for p in (a or []) if p not in (b or [])] if field != 'selection' else []
        removed = [p for p in (b or []) if p not in (a or [])] if field != 'selection' else []
        if action in {'shortlist_add', 'shortlist_remove', 'exclude', 'restore'}:
            added = [p for p in added if p in ids] if action in {'shortlist_add', 'exclude'} else []
            removed = [p for p in removed if p in ids] if action in {'shortlist_remove', 'restore'} else []
        unchanged = [p for p in ids if p not in added + removed] if field != 'selection' else (list(ids) if b == a else [])
        operations.append({'action': action, 'requested_ids': ids, 'field': field,
                           'before': deepcopy(b), 'after': deepcopy(a), 'added_ids': added,
                           'removed_ids': removed, 'unchanged_ids': unchanged,
                           'reason': ('not_in_shortlist' if action == 'shortlist_remove' and unchanged and all(p not in (b or []) for p in unchanged) else
                                      'already_present' if action == 'shortlist_add' and unchanged and all(p in (b or []) for p in unchanged) else
                                      'no_effect' if b == a else None),
                           'status': ('changed' if added or removed else 'no_change') if action in {'shortlist_add', 'shortlist_remove', 'exclude', 'restore'} else ('no_change' if b == a else 'changed')})
    if not operations:
        return None
    review = any(o['status'] == 'no_change' or o['unchanged_ids'] for o in operations)
    return {'receipt_id': json.dumps([turn_id, group_id]), 'group_id': group_id,
            'task_id': before['id'], 'scope': scope, 'revision_before': before['revision'],
            'revision_after': after['revision'], 'before': snapshot(before, scope), 'after': snapshot(after, scope),
            'status': 'changed' if any(o['status'] == 'changed' for o in operations) else 'no_change',
            'requires_review': review, 'operations': operations}


def decision_context(state, catalog, *, events=False):
    def product(pid):
        p = catalog._products.get(pid, {})
        return {'id': pid, 'name': p.get('title', pid)}
    result = {'task_id': state['id'], 'category': state['category'], 'revision': state['revision']}
    for scope in ['formal', 'hypothetical']:
        if scope == 'hypothetical' and not state.get('exploration'):
            continue
        d = bucket(state, scope)
        result[scope] = {'shortlist': [product(p) for p in d.get('shortlist_ids', [])],
                         'selection': product(d['selection']['product_id']) if d.get('selection') else None,
                         'focus': [product(p) for p in d.get('focus_ids', [])],
                         'excluded': [product(p) for p in ((state.get('exploration') or {}).get('excluded', {}) if scope == 'hypothetical' else state.get('excluded', {}))]}
        if scope == 'hypothetical':
            result[scope]['exploration_id'] = state['exploration']['id']
            result[scope]['stale'] = state['exploration']['base_requirements_version'] != state['requirements_version']
        if events:
            result[scope]['recent_events'] = deepcopy(d.get('events', [])[-8:])
            result[scope]['omitted_events'] = max(0, len(d.get('events', [])) - 8)
    return result


def validate_review(turn, answer):
    from .state import InvalidChange
    receipts = getattr(turn, 'decision_effects', {})
    refs = answer.decision_receipt_refs
    if len(refs) != len(set(refs)) or any(r not in receipts for r in refs):
        raise InvalidChange('decision_receipt_refs must reference unique receipts from this turn')
    required = {k for k, v in receipts.items() if v['requires_review']}
    if answer.kind in {'answered', 'recommendation', 'no_match'} and required - set(refs):
        raise InvalidChange('decision_effect_review: no/partial change is not completion. Inspect decision_effects and current decision_context; correct wrong targets with a NEW group_id, or explicitly acknowledge existing state using decision_receipt_refs. Required refs: '+json.dumps(sorted(required)))


def render(turn):
    from .presentation import product_name
    receipts = getattr(turn, 'decision_effects', {})
    lines = []
    def names(ids):
        return '、'.join(product_name(turn.catalog, pid) for pid in ids)
    for r in receipts.values():
        prefix = '临时方案：' if r['scope'] == 'hypothetical' else ''
        if r.get('replayed'):
            lines.append(prefix+'该操作已处理，本次没有重复执行。')
            continue
        for o in r['operations']:
            if o['action'] in {'shortlist_add', 'shortlist_remove'}:
                if o['added_ids']:
                    lines.append(prefix+'已加入备选：'+names(o['added_ids'])+'。')
                if o['removed_ids']:
                    lines.append(prefix+'已从备选移除：'+names(o['removed_ids'])+'。')
                if o['unchanged_ids']:
                    for pid in o['unchanged_ids']:
                        present = pid in (o['before'] or [])
                        if o['action'] == 'shortlist_remove' and not present:
                            text = '操作时不在备选中，本次未移除'
                        elif o['action'] == 'shortlist_add' and present:
                            text = '操作时已在备选中，本次未重复添加'
                        else:
                            text = '本组操作后备选状态未变化'
                        lines.append(prefix+names([pid])+'：'+text+'。')
            elif o['status'] == 'no_change':
                lines.append(prefix+'该项决定操作未改变原有状态。')
    return '\n'.join(lines)


def attach(turn, result):
    effects = list(getattr(turn, 'decision_effects', {}).values())
    if not effects or result.get('kind') == 'stopped':
        return result
    result['decision_effects'] = deepcopy(effects)
    text = render(turn)
    if text:
        result['message'] = text + '\n' + result['message']
    result['expression_review'] = {'accepted': False, 'status': 'protected_decision_effect_delivery'}
    return result
