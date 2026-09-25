"""Budget-expansion requests and retrieval coverage, derived from executed state/tools."""
from copy import deepcopy
from decimal import Decimal
import re
from .state import InvalidChange


def budget(requirements):
    value = requirements.get('budget', {})
    if value.get('status') == 'active' and value.get('strength') == 'hard':
        return value.get('value')
    return None


def effective(state, scope):
    result = deepcopy(state.get('requirements', {}))
    if scope == 'hypothetical':
        ex = state.get('exploration')
        if not ex or ex['base_requirements_version'] != state['requirements_version']:
            return None
        result.update(ex['overrides'])
    return result


def sync_requests(turn, before):
    """Additive optional metadata; legacy tasks derive an active overlay's origin."""
    state = turn.state()
    if not state:
        return
    legacy = 'discovery_requests' not in state
    original = deepcopy(state.get('discovery_requests', {}))
    requests = deepcopy(original)
    for scope in ['formal', 'hypothetical']:
        current = effective(state, scope)
        if current is None:
            requests.pop(scope, None)
            continue
        target = budget(current)
        previous_reqs = effective(before, scope)
        # A new overlay starts from formal requirements; a replacement starts
        # from the last valid overlay, rather than reverting to its formal base.
        previous = budget(previous_reqs or before.get('requirements', {}))
        old = requests.get(scope)
        action = getattr(turn, 'discovery_budget_actions', {}).get(scope)
        # Restoring a ceiling is not a new request to explore higher prices.
        if action == 'undo':
            requests.pop(scope, None)
            continue
        if target != previous and not (old and old.get('created_turn') == turn.turn_id and old.get('upper') == target):
            if action in {'apply', 'explore'} and target and previous and target['currency'] == previous['currency'] == 'USD' and Decimal(target['amount']) > Decimal(previous['amount']):
                requests[scope] = {'lower': deepcopy(previous), 'upper': deepcopy(target),
                                   'created_turn': turn.turn_id, 'delivered': False}
            else:
                requests.pop(scope, None)
        elif old and old['upper'] != target:
            requests.pop(scope, None)
        if legacy and scope == 'hypothetical' and scope not in requests and not old:
            formal = budget(state['requirements'])
            if target and formal and target['currency'] == formal['currency'] == 'USD' and Decimal(target['amount']) > Decimal(formal['amount']):
                requests[scope] = {'lower': deepcopy(formal), 'upper': deepcopy(target),
                                   'created_turn': state['exploration']['turn_id'], 'delivered': False}
    if requests != original:
        with turn.store.db:
            state['discovery_requests'] = requests
            turn.store._save(state)


def record_only(turn):
    return bool(re.search(r'只记录|仅记录|只保存|仅保存', turn.text))


def preferred_scope(turn):
    if 'hypothetical' in turn.applied_scopes and any(g.action == 'explore' for g in turn.plan.groups):
        return 'hypothetical'
    return turn.plan.recommendation_scope or ('hypothetical' if 'hypothetical' in turn.active_scopes() else 'formal')


def coverage(turn, scope):
    """A broader exhaustive retrieval can be reused, independent of ranking scope."""
    state = turn.state()
    target = budget(turn.requirements(scope))
    excluded = set(state['excluded'])
    if scope == 'hypothetical':
        excluded.update(state['exploration'].get('excluded', {}))
    for entry in reversed(state.get('action_history', [])):
        if entry.get('action') != 'search' or entry.get('coverage') != 'exhaustive_structured_filter':
            continue
        filters = entry.get('filters', {})
        if entry.get('catalog_version') != turn.catalog.version or filters.get('category') != state['category']:
            continue
        if 'retrieved_ids' not in entry or not set(filters.get('excluded_ids', [])) <= excluded:
            continue
        ceiling = filters.get('budget')
        if ceiling is not None and (target is None or ceiling['currency'] != target['currency'] or Decimal(ceiling['amount']) < Decimal(target['amount'])):
            continue
        return entry
    return None


def progress(turn, scope):
    state = turn.state() or {}
    request = state.get('discovery_requests', {}).get(scope)
    if not request or request.get('delivered') or scope not in turn.active_scopes():
        return {}
    # An explicit minimum-price objective takes precedence over inferred interest
    # in higher-price options. The existing cheapest gate still requires a search.
    if turn.plan.scope_ids is not None or record_only(turn) or turn.plan.stop_requested or turn.plan.cheapest_requested:
        return {}
    receipt = coverage(turn, scope)
    result = {**deepcopy(request), 'scope': scope, 'coverage_complete': bool(receipt),
              'eligible_new_ids': [], 'unknown_new_ids': [], 'violated_new_ids': [],
              'instruction': 'Budget expansion asks for new choices. Search the effective scope with query="" if coverage is missing. Inspect representative eligible_new_ids and actually show at least one in the final product_ids; existing products may be comparisons. Do not merely acknowledge or repeat the old pool.'}
    if not receipt:
        return result
    requirements = turn.requirements(scope)
    excluded = set(state['excluded'])
    if scope == 'hypothetical':excluded.update(state['exploration'].get('excluded', {}))
    lower, upper = Decimal(request['lower']['amount']), Decimal(request['upper']['amount'])
    for pid in receipt['retrieved_ids']:
        if pid in excluded:
            continue
        product = turn.catalog._products[pid]
        price = product['price']
        if price['currency'] != request['upper']['currency'] or not lower < Decimal(str(price['amount'])) <= upper:
            continue
        q = turn.catalog.qualification(pid, requirements)
        key = 'violated_new_ids' if q['violated'] else 'unknown_new_ids' if q['unknown'] or q['conflict'] else 'eligible_new_ids'
        result[key].append(pid)
    return result


def validate_delivery(turn, answer):
    """State-only queries and bounded comparisons never trigger discovery work."""
    if not turn.task_id or turn.plan.scope_ids is not None or record_only(turn) or turn.plan.stop_requested:
        return None
    # Existing finish validation still requires a real open clarification gap.
    # Asking for required information must not consume an expansion request.
    if answer.kind == 'needs_user' or (turn.group_errors and not answer.product_ids):
        return None
    expected = preferred_scope(turn)
    p = progress(turn, expected)
    product_reply = answer.kind == 'recommendation' or bool(answer.product_ids)
    # Budget-only queries in later turns remain state queries. New expansion
    # operations default to showing alternatives unless explicitly record-only.
    budget_operation = any(g.action in {'apply', 'explore'} and any(op.target == 'requirements' and op.key == 'budget' for op in g.operations) for g in turn.plan.groups)
    triggered = p and p['created_turn'] == turn.turn_id and budget_operation and bool(turn.display_snapshot)
    if not product_reply and not triggered:
        return None
    if product_reply and answer.scope != expected:
        raise InvalidChange(f'discovery_scope: current recommendation scope is {expected}; use the same scope for search, assessment and delivery. Set process_turn recommendation_scope=formal only for an explicit formal request.')
    if not p:
        return None
    if not p['coverage_complete']:
        raise InvalidChange(f'discovery_coverage: prior search does not cover this expansion; call search_products(query="", scope="{expected}") then assess the returned pool. Do not finish with only a budget acknowledgement.')
    eligible = set(p['eligible_new_ids'])
    if eligible and not eligible.intersection(answer.product_ids):
        raise InvalidChange('discovery_delivery: show at least one verified newly affordable option in product_ids, not just old products or a state confirmation. Inspect and compare these candidates: '+str(p['eligible_new_ids']))
    return p


def complete(turn, answer, result, checked):
    if not checked:
        return
    if not checked['eligible_new_ids']:
        if checked['unknown_new_ids']:
            message = '提高预算后新增价位的商品，现有资料仍不足以确认符合你的其他要求，暂不把它们作为合格推荐。'
        elif checked['violated_new_ids']:
            message = '已查看提高预算后的新增价位，商品都不符合你保留的其他必需条件。'
        else:
            message = '当前商品资料中，提高预算后的新增价位没有可新增推荐的商品。'
        result['message'] += '\n' + message
    else:
        result['message'] += '\n以上包含提高预算后新增的可选商品；原预算内的商品仍可作为对照，价格更高不代表一定更适合。'
    with turn.store.db:
        state = turn.state()
        request = state.get('discovery_requests', {}).get(checked['scope'])
        if request and request['created_turn'] == checked['created_turn'] and request['upper'] == checked['upper']:
            request['delivered'] = True
            request['delivered_turn'] = turn.turn_id
            request['displayed_new_ids'] = [pid for pid in answer.product_ids if pid in checked['eligible_new_ids']]
            turn.store._save(state)
