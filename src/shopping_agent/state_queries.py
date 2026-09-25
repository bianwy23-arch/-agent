"""Read-only, task-scoped state queries and protected delivery composition."""
from copy import deepcopy
from .state import InvalidChange
from .state_facts import state_facts


def validate_queries(turn, queries):
    """Reject foreign IDs before any process operation can write."""
    for query in queries:
        if query.task_id:
            if query.task_id not in turn.conversation.get('task_ids', []):
                raise InvalidChange('state query task does not belong to this conversation')
            if query.category and turn.store.get(query.task_id)['category'] != query.category:
                raise InvalidChange('state query category does not match task')


class StateView:
    def __init__(self, turn, state):
        self.catalog, self.saved = turn.catalog, state

    def state(self):
        return self.saved

    def requirements(self, scope):
        result = deepcopy(self.saved['requirements'])
        if scope == 'hypothetical':
            result.update(self.saved['exploration']['overrides'])
        return result


def query_results(turn):
    queries = getattr(turn, 'state_queries', [])
    validate_queries(turn, queries)
    # Load each task once so fields within this delivery share the same state.
    states = {tid: turn.store.get(tid) for tid in turn.conversation.get('task_ids', [])}
    labels = {p['category_id']: p['category_name'] for p in turn.catalog._products.values()}
    rows = []
    for query in queries:
        ids = [query.task_id] if query.task_id else [tid for tid, st in states.items() if st['category'] == query.category]
        row = {'task_id': ids[0] if len(ids) == 1 else None,
               'category': states[ids[0]]['category'] if len(ids) == 1 else query.category,
               'scope': query.scope, 'requested_fields': list(query.fields), 'facts': {}, 'status': 'resolved'}
        if len(ids) != 1:
            row.update(status='unresolved', reason='task_not_found' if not ids else 'ambiguous_task',
                       candidate_task_ids=ids,
                       clarification='本会话没有对应任务，无法读取这些状态。' if not ids else '本会话有多个同品类任务，请明确要查询哪一个任务：'+'、'.join(ids)+'。')
        else:
            facts = state_facts(StateView(turn, states[ids[0]]), query.scope)
            for field in query.fields:
                if field in facts:
                    row['facts'][field] = facts[field]
                elif query.scope == 'hypothetical' and facts.get('exploration_status') != '临时方案正在生效，正式要求独立保留。':
                    row['facts'][field] = facts.get('exploration_status', '临时状态不可用。') + '无法提供该临时方案的此项状态。'
                elif field == 'budget':
                    row['facts'][field] = facts['formal_budget']
                elif field == 'selection_qualification':
                    row['facts'][field] = facts['selection'] + '没有选定商品可核对资格。'
                else:
                    raise InvalidChange('state query field has no authoritative result: '+field)
        row['label'] = labels.get(row['category'], row['category'])
        rows.append(row)
    return rows


FIELD_LABELS = {'budget':'预算','formal_budget':'正式预算','temporary_budget':'临时预算',
    'shortlist':'备选','selection':'选定','selection_qualification':'选定商品资格',
    'preferences':'偏好','excluded':'排除','purchase':'购买状态','exploration_status':'临时方案状态'}


def render_queries(rows):
    parts = []
    for row in rows:
        label = row['label']
        scope = '临时方案' if row['scope'] == 'hypothetical' else '正式状态'
        # IDs distinguish multiple tasks of the same category without changing active task.
        same_category = sum(r['category'] == row['category'] and r['task_id'] != row['task_id'] for r in rows)
        task = '（任务 '+row['task_id']+'）' if same_category and row['task_id'] else ''
        lines = [label+task+' · '+scope]
        if row['status'] == 'unresolved':
            lines.append(row['clarification'])
        else:
            lines.extend(FIELD_LABELS[key]+'：'+value for key, value in row['facts'].items())
        parts.append('\n'.join(lines))
    return '\n\n'.join(parts)


def append_queries(turn, result):
    if not getattr(turn, 'state_queries', []) or result['kind'] == 'stopped':
        return result
    rows = query_results(turn)
    result['state_query_results'] = rows
    result['state_query_status'] = 'partial' if any(r['status'] == 'unresolved' for r in rows) else 'complete'
    result['message'] += '\n\n'+render_queries(rows)
    result['expression_review'] = {'accepted':False, 'status':'protected_state_query_delivery'}
    return result


def pure_query_answer(turn, answer):
    rows = query_results(turn)
    partial = any(r['status'] == 'unresolved' for r in rows)
    expected = 'needs_user' if partial else 'answered'
    if answer.kind != expected:
        raise InvalidChange('state query requires kind='+expected+'; use returned query results')
    if answer.answer_purpose not in ({None} if partial else {None, 'state'}):
        raise InvalidChange('pure state query uses state purpose only when answered')
    if any((answer.decision_receipt_refs, answer.product_ids, answer.claims, answer.citations, answer.assessment_id,
            answer.state_fact_refs, answer.explanation_topics, answer.limit_fact_refs,
            answer.answer_boundary, answer.recommendation_proposal, answer.recommendation_reasons,
            answer.question, answer.unresolved)):
        raise InvalidChange('pure state query uses program query results, without other delivery contracts')
    result = answer.model_dump(mode='json')
    result.update(message=render_queries(rows), state_query_results=rows,
                  state_query_status='partial' if partial else 'complete',
                  task_id=turn.task_id, turn_id=turn.turn_id, state=turn.state(),
                  display_id=None, product_ids=[], product_cards=[], selection_ack=None,
                  comparison_view=None, feedback=[], price_notice='', group_errors=[],
                  expression_review={'accepted':False,'status':'protected_state_query_delivery'})
    return result
