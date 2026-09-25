"""Bounded epistemic answers. No free prose or model-reported evidence status."""
from .state import InvalidChange

EXTRA_LABELS = {
    'comfort': '舒适度', 'durability': '耐用性',
    'sound_quality': '音质', 'stain_resistance': '耐脏性',
    'noise_cancellation': '降噪表现',
}


def field_label(field):
    from .delivery import LABELS
    label = {**LABELS, **EXTRA_LABELS}.get(field)
    if not label:
        raise InvalidChange('unsupported answer dimension: ' + field)
    return label


def evidence_status(turn, pid, field):
    """Only explicit current-turn reads support missing/conflicting declarations."""
    inspected = turn.inspected.get(pid, {})
    fact = inspected.get('facts', {}).get(field)
    if fact is None:
        return 'uninspected'
    normalized = inspected.get('normalized', {}).get(field, {})
    if fact.get('status') == 'conflict' or normalized.get('status') == 'conflict':
        return 'conflict'
    if fact.get('status') == 'unknown':
        return 'missing'
    return 'available'


def render_boundary(turn, answer):
    boundary = answer.answer_boundary
    if boundary is None:
        return ''
    if len(boundary.question_quote.strip()) < 2 or boundary.question_quote.strip() not in turn.text:
        raise InvalidChange('answer_boundary must quote the current user question')
    if answer.state_fact_refs or answer.limit_fact_refs:
        raise InvalidChange('answer_boundary cannot bypass state or live capability answers')
    if turn.applied_scopes:
        raise InvalidChange('answer_boundary is a question response, not a state update acknowledgement')
    target = field_label(boundary.target_field)
    basis = [field_label(f) for f in boundary.basis_fields]
    if boundary.target_field in boundary.basis_fields or len(set(boundary.basis_fields)) != len(basis):
        raise InvalidChange('answer_boundary requires distinct basis and target dimensions')
    ids = boundary.product_ids
    if len(ids) != len(set(ids)):
        raise InvalidChange('answer_boundary products must be distinct')
    state = turn.state() or {}
    excluded = set(state.get('excluded', {}))
    if answer.scope == 'hypothetical':
        excluded.update((state.get('exploration') or {}).get('excluded', {}))
    for pid in ids:
        if pid not in state.get('candidates', {}) or pid in excluded:
            raise InvalidChange('answer_boundary requires current allowed products')
        if turn.plan.scope_ids is not None and pid not in turn.plan.scope_ids:
            raise InvalidChange('answer_boundary outside user scope')
    if not ids:
        if not basis:
            raise InvalidChange('a general inference question requires basis_fields')
        if answer.product_ids or answer.claims or answer.citations or answer.assessment_id:
            raise InvalidChange('product evidence requires product-scoped answer_boundary')
        return f'仅凭{"、".join(basis)}这一类信息，还不能确认{target}；需要与{target}直接相关的依据。这不是说两者毫无关系，而是不能用前者代替对后者的判断。'
    referenced = set(answer.product_ids) | {p for c in answer.claims for p in c.product_ids} | {c.product_id for c in answer.citations}
    if referenced - set(ids):
        raise InvalidChange('answer_boundary must cover all answer products')
    from .presentation import product_name
    rows = []
    statuses = []
    for pid in ids:
        status = evidence_status(turn, pid, boundary.target_field)
        statuses.append(status)
        wording = {
            'uninspected': '本轮还未查看对应资料，不能把尚未检查说成没有证据',
            'missing': '本轮已查看，但未取得可核验的对应信息；这不代表实际表现差',
            'conflict': '现有资料存在冲突，需要先核对冲突再判断',
            'available': '已有对应资料，可按下列来源内容判断；资料标注不等于独立实测',
        }[status]
        rows.append(f'{product_name(turn.catalog, pid)}：{wording}。')
        if status == 'available' and not any(c.kind == 'attribute_fact' and c.field == boundary.target_field and pid in c.product_ids for c in answer.claims):
            raise InvalidChange('target evidence exists; include its attribute_fact rather than claiming missing evidence')
    prefix = f'仅凭{"、".join(basis)}，还不能确认这些商品的{target}。' if basis else f'关于这些商品的{target}：'
    if all(s == 'missing' for s in statuses):
        prefix += f'目前查看到的资料不足以给出可靠的{target}结论。'
    return prefix + '\n' + '\n'.join(rows)
