"""Quantity intent and evidence-scoped, bounded recommendation delivery."""
import re
from copy import deepcopy
from .state import InvalidChange

DISPLAY_LIMIT = 10
DIGITS = dict(zip('零一二三四五六七八九', range(10))) | {'两': 2}

def number(text):
    if re.fullmatch(r'[0-9]+', text):
        return int(text) if len(text) <= 100 else None
    total = section = current = 0
    for char in text:
        if char in DIGITS:
            current = DIGITS[char]
        elif char in '十百千':
            section += (current or 1) * {'十':10, '百':100, '千':1000}[char]
            current = 0
        elif char == '万':
            total += (section + current) * 10000
            section = current = 0
        else:
            return None
    return total + section + current

def parse(text):
    empty = {'status':'unspecified', 'count':None, 'relation':'exact', 'quote':None}
    trigger = re.search(r'推荐|只给|选|改成|改为|调整为|想买|想要|帮我挑|给我|再来', text)
    # Explicit product counts do not depend on a recommendation verb whitelist.
    # Keep the legacy verb gate only for ambiguous general classifiers (个/把).
    matches = list(re.finditer(r'(?P<rel>最多|至多|不超过|至少|不少于)?\s*(?P<num>-?[0-9]+|[零一二两三四五六七八九十百千万]+)\s*[款个把]', text))
    values = []
    for m in matches:
        prefix = text[max(0,m.start()-8):m.start()]
        if re.search(r'(?:不要|这|那|第|前|后|已有|买过|看过|刚才的|之前的)\s*$', prefix):
            continue
        if not trigger and not m.group().rstrip().endswith('款'):
            continue
        n = number(m['num'])
        relation = 'at_most' if m['rel'] in ('最多','至多','不超过') else 'at_least' if m['rel'] in ('至少','不少于') else 'exact'
        value = {'status':'explicit' if n is not None and n >= 0 else 'ambiguous', 'count':n if n is not None and n >= 0 else None, 'relation':relation, 'quote':m.group().strip()}
        if re.search(r'改成|改为|调整为', prefix):
            values = []
        values.append(value)
    if not values:
        if re.search(r'(?:若干|几)[款个把]',text):
            return empty | {'quote':text}
        if re.search(r'(?:数十|数百|上百|几十|几百)[款把]',text):
            return empty | {'status':'ambiguous','quote':text}
        return empty
    if len({(v['count'],v['relation']) for v in values}) > 1:
        return empty | {'status':'ambiguous','quote':text}
    return values[-1]

def effective(turn):
    request = parse(turn.text)
    if request['status'] != 'unspecified' or request['quote']:
        return request
    for text in reversed(list((turn.state() or {}).get('turns', {}).values())):
        request = parse(text)
        if request['status'] != 'unspecified' or request['quote']:
            return request
    return request

def context(turn, scope):
    state = turn.state()
    excluded = set(state['excluded'])
    if scope == 'hypothetical':
        excluded.update((state.get('exploration') or {}).get('excluded', {}))
    return {'requirements':deepcopy(turn.requirements(scope)), 'excluded':sorted(excluded), 'scope_ids':sorted(turn.plan.scope_ids) if turn.plan.scope_ids is not None else None}

def facts(turn, scope):
    request = effective(turn)
    current = context(turn, scope)
    valid = [s for s in turn.searches if s.get('scope') == scope and s.get('quantity_context') == current and s.get('coverage') == 'exhaustive_structured_filter']
    full = valid[-1] if valid else None
    ids = set(c['id'] for c in full['items']) if full else set((turn.state() or {}).get('candidates', {}))
    ids -= set(current['excluded'])
    if current['scope_ids'] is not None:
        ids &= set(current['scope_ids'])
    eligible, unresolved = [], []
    for pid in ids:
        q = turn.catalog.qualification(pid, turn.requirements(scope))
        if q['violated']:
            continue
        (unresolved if q['unknown'] or q['conflict'] else eligible).append(pid)
    return {'request':request, 'display_limit':DISPLAY_LIMIT, 'coverage_complete':bool(full) or (current['scope_ids'] is not None and set(current['scope_ids']) <= set((turn.state() or {}).get('candidates', {}))), 'qualified_count':len(eligible), 'unresolved_count':len(unresolved), 'inspected_count':len(set(eligible) & set(turn.inspected))}

def validate(turn, answer):
    f = facts(turn, answer.scope)
    request = f['request']; n = request['count']; actual = len(answer.product_ids)
    turn.quantity_summary = ''
    if request['status'] == 'ambiguous':
        raise InvalidChange('Ambiguous recommendation quantity: clarify with needs_user before recommending')
    if actual > DISPLAY_LIMIT:
        raise InvalidChange('Single-turn display limit is 10; preserve the original requested quantity')
    if n is None:
        return
    relation = request['relation']
    if relation in ('exact','at_most') and actual > n:
        raise InvalidChange('user explicitly requested exactly ' + str(n) + ' products; recommendation exceeds requested quantity')
    if relation == 'at_most' or actual >= n:
        return
    target = min(n, DISPLAY_LIMIT)
    complete = f['coverage_complete']
    total = f['qualified_count']
    known_target = min(target,total) if complete else target
    budget = getattr(turn,'quantity_execution_limited',False)
    if actual < known_target and not budget:
        raise InvalidChange('user explicitly requested exactly ' + str(n) + ' products in this task. Inspect and include enough hard-qualified alternatives, or report a verified pool shortfall; do not arbitrarily reduce the delivery count.')
    if not actual:
        raise InvalidChange('No verified products: use the existing no_match/data_limited boundary')
    text = f'你希望推荐{"至少" if relation == "at_least" else ""}{n}款。'
    if complete:
        text += f'当前收录目录及本次筛选范围内，已确认符合硬条件的商品有{total}款。'
        if f['unresolved_count']:
            text += f'另有{f["unresolved_count"]}款仍有条件待核实，尚不能确认最终合格总量。'
        elif total < n:
            text += f'这个范围内不足{n}款。'
    else:
        text += f'目前已找到{total}款符合硬条件的候选，搜索尚未完整覆盖，不能确定总量。'
    text += f'本轮展示已查看资料的{actual}款。'
    if actual < known_target and budget:
        text += '本轮已到执行预留边界，先交付已核实结果。'
    elif n > DISPLAY_LIMIT and actual == DISPLAY_LIMIT:
        text += f'每轮最多展示{DISPLAY_LIMIT}款。'
    if total > actual:
        text += '其余候选尚未全部展示。'
    text += f'本轮展示数量比请求少 {n-actual} 款。以上范围不代表全市场。'
    turn.quantity_summary = text
