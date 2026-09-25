"""Authoritative state facts for model-composed, checked status answers."""
import re
from .state import InvalidChange


def preference_sentence(requirements):
    """Expose saved soft requirements, never invent preferences from a scenario."""
    from .presentation import field_label
    parts = []
    for key, req in requirements.items():
        if req.get('strength') != 'soft':
            continue
        label = field_label(req.get('field') or key)
        if req.get('status') == 'no_preference':
            parts.append(label + '：明确没有特别偏好')
            continue
        if req.get('status') != 'active':
            continue
        expression = req.get('expression') or {}
        kind = expression.get('kind')
        if kind == 'minimize':
            meaning = '更轻一些' if (req.get('field') or key) == 'weight' else '倾向更低的数值'
        elif kind == 'maximize':
            meaning = '倾向更高的数值'
        elif kind == 'prefer_value':
            meaning = str(expression['target'])
        elif kind == 'target':
            target = expression['target']
            operator = {'lte':'不超过', 'gte':'不少于', 'eq':'等于', 'lt':'小于', 'gt':'大于', 'contains':'包含', 'not_contains':'不包含'}.get(target['operator'], target['operator'])
            meaning = operator + str(target['value']) + (' ' + target['unit'] if target.get('unit') else '')
        elif kind == 'qualitative':
            meaning = expression['text']
        else:
            meaning = (req.get('source') or {}).get('quote') or str(req.get('value') or '具体内容未记录')
        parts.append(label + '：' + meaning)
    return '已记录的偏好：' + '；'.join(parts) + '。' if parts else '目前没有记录明确偏好，这不代表你不在意这些方面。'


def state_facts(turn, scope="formal"):
    state = turn.state()
    if not state:
        return {}
    formal_budget = state['requirements'].get('budget', {})
    def budget_sentence(requirement, prefix):
        if requirement.get('status') == 'active':
            value = requirement['value']
            return prefix + '为 ' + str(value['amount']) + ' ' + value['currency'] + '。'
        return prefix + '未设置有效上限。'
    facts = {'formal_budget': budget_sentence(formal_budget, '正式预算')}
    exploration = state.get('exploration')
    if not exploration:
        facts.update(exploration_status='当前没有临时方案。',
                     temporary_budget='当前没有生效的临时预算：当前没有临时方案。')
    elif exploration['base_requirements_version'] != state['requirements_version']:
        facts.update(exploration_status='临时方案已失效，需要重新评估。',
                     temporary_budget='临时方案已失效，其中的预算不能作为当前有效临时预算。')
    else:
        facts.update(exploration_status='临时方案正在生效，正式要求独立保留。',
                     temporary_budget=budget_sentence(exploration['overrides'].get('budget', formal_budget), '临时预算'))
    if scope == 'hypothetical' and (not exploration or exploration['base_requirements_version'] != state['requirements_version']):
        return facts
    requirements = turn.requirements(scope)
    current = state['exploration'] if scope == 'hypothetical' else state
    decision = current.get('decision', {})
    from .presentation import product_name
    names = lambda ids: '；'.join(product_name(turn.catalog, p) for p in ids)
    excluded = set(state.get('excluded', {}))
    if scope == 'hypothetical':
        excluded.update(current.get('excluded', {}))
    facts['preferences'] = preference_sentence(requirements)
    facts['excluded'] = '你已排除：' + names(sorted(excluded)) + '。' if excluded else '你目前没有排除商品。'
    ids = decision.get('shortlist_ids', [])
    selection = decision.get('selection') or {}
    facts.update({'shortlist': '你保留的是：'+names(ids)+'。' if ids else '你目前没有保留商品。',
             'selection': '你目前尚未选定商品。',
             'purchase': '保留或选定不等于购买；本系统没有执行下单或付款。'})
    pid = selection.get('product_id')
    if pid:
        name = names([pid])
        facts['selection'] = ('你已明确选定：' if selection.get('commitment')=='confirmed' else '你目前倾向的是：')+name+'。'
        # Saved selection queries do not need a fresh tool read to describe source qualification.
        q = turn.catalog.qualification(pid, requirements)
        status = '不符合你现在的要求' if q['violated'] else '是否符合其他要求，还需要确认' if q['unknown'] or q['conflict'] else '符合你现在的要求'
        facts['selection_qualification'] = '这款商品'+status+'。你的选择仍保留。'
        budget_check = q['checks'].get('budget')
        if budget_check:
            price = budget_check['price']
            limit = requirements['budget']['value']
            comparison = {'violated': '已超预算',
                          'satisfied': '符合预算',
                          'unknown': '币种无法直接比较，因此尚不能确认符合预算'}[budget_check['status']]
            facts['selection_qualification'] += (f"历史参考价 {price['amount']} {price['currency']}，"
                f"当前预算为 {limit['amount']} {limit['currency']}；{comparison}。")

    budget = requirements.get('budget', {})
    if budget.get('status') == 'active':
        v = budget['value'];facts['budget'] = ('临时预算为 ' if scope=='hypothetical' else '当前预算为 ')+str(v['amount'])+' '+v['currency']+'。'
    return facts


def requested_state_facts(text):
    """A narrow safeguard for explicit personal-state questions, not concept questions."""
    required = set()
    if re.search(r'是什么意思|有什么区别|有何区别|什么是', text):
        return required
    # Narrow completeness checks for concrete scoped queries, not concept explanations.
    if re.search(r'多少|几|有没有|是否|生效了吗|还在吗', text):
        if re.search(r'临时.{0,12}预算|预算.{0,12}临时', text):
            required.add('temporary_budget')
        if re.search(r'正式.{0,12}预算|预算.{0,12}正式', text):
            required.add('formal_budget')
        if '临时方案' in text and not required:
            required.add('exploration_status')
    if not re.search(r'我|刚才|之前|当前|现在',text):
        return required
    query = re.search(r'哪|什么|是否|吗|有没有|告诉我',text)
    if not query:
        return required
    if re.search(r'保留|备选',text):required.add('shortlist')
    if '偏好' in text:required.add('preferences')
    if '排除' in text:required.add('excluded')
    if required & {'preferences','excluded'} and '预算' in text and not re.search(r'临时|正式',text):required.add('budget')
    if re.search(r'选定|选的是|选了|选择的是|选择了',text):required.add('selection')
    if required and re.search(r'购买|买了|下单|付款',text):required.add('purchase')
    if 'selection' in required and re.search(r'符合|超出|超预算',text):required.add('selection_qualification')
    return required


def verify_state_answer(turn, answer):
    required = requested_state_facts(turn.text)
    # Explicit task-scoped queries are rendered as a separate protected block.
    # Do not demand those fields again from the active task's legacy answer.
    covered = {key for query in getattr(turn, 'state_queries', []) for key in query.fields}
    if covered & {'formal_budget', 'temporary_budget'}:
        covered.add('budget')
    required -= covered
    if answer.kind != 'answered':
        if answer.state_fact_refs:raise InvalidChange('state_fact_refs requires answered')
        return False
    facts = state_facts(turn,answer.scope)
    if 'selection_qualification' not in facts:required.discard('selection_qualification')
    refs = answer.state_fact_refs
    budget_ref = 'temporary_budget' if answer.scope == 'hypothetical' else 'formal_budget'
    if budget_ref in refs:
        required.discard('budget')
    if not refs and not required:return False
    if not refs or required-set(refs):
        raise InvalidChange('Answer the actual saved state before concepts. Required state_fact_refs: '+str(sorted(required))+'. Select the relevant keys from state_facts; the program renders their current values.')
    if len(set(refs))!=len(refs) or set(refs)-set(facts):raise InvalidChange('Unknown or duplicate state fact reference')
    if answer.claims or answer.product_ids:raise InvalidChange('State answer uses state_fact_refs, without product claims or recommendation product_ids')
    # message is a draft, not authoritative state data. It cannot alter or add
    # facts to delivery. Natural prose must use the separately reviewed channel.
    return True


def render_state_answer(turn, answer):
    """Render validated references from live state, never from model prose."""
    facts = state_facts(turn, answer.scope)
    return '\n'.join(facts[key] for key in answer.state_fact_refs)
