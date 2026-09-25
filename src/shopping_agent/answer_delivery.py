"""Answered delivery contracts. Provenance checks are not a prose truth classifier."""
import re
from .state import InvalidChange
from .presentation import claim_text
from .state_facts import render_state_answer

EXPLANATIONS = {
    "hard_vs_soft": "硬条件决定主推荐资格；软偏好用于比较取舍，不会自动变成硬筛选。",
    "unknown_vs_no_preference": "未知表示尚不确定；明确无偏好表示这项不参与排序，也不会由旧假设自动恢复。",
    "selection_vs_purchase": "推荐是系统建议；选定是你的决定。选定不等于下单、付款或已完成购买。",
    "historical_prices": "这里显示历史美元价格，不代表当前报价或库存，也不会自行换算人民币。",
}


def validate_visible_message(message):
    text = message.strip()
    # Deterministic shape check only, not a general semantic completeness judge.
    if not text or re.fullmatch(r"[\s#*>_-]*(?:当前要求|临时方案已记录)[：:。\s]*", text):
        raise InvalidChange("empty_delivery: provide a substantive reply; a state heading alone is not an answer")
    return text


def answered_message(turn, answer, claims, state_verified, live_verified):
    """Select an explicit delivery contract after all existing fact gates ran."""
    purpose = answer.answer_purpose
    limits_requested = '限制' in turn.text
    cups_requested = bool(re.search(r'cups?|杯', turn.text, re.I) and re.search(r'换算|升|liters?', turn.text, re.I))
    if purpose is None and state_verified:
        # Legacy callers may carry unused concept topics; render state refs only.
        return validate_visible_message(render_state_answer(turn, answer))
    evidence = bool(answer.answer_boundary or answer.claims or answer.product_ids or answer.citations or
                    answer.assessment_id or answer.explanation_topics or answer.limit_fact_refs)
    if purpose is None:
        # Backward compatibility is bounded to an actual evidence or state path.
        purpose = "state" if state_verified else "evidence" if evidence or limits_requested or cups_requested else "state"
    if purpose == "interaction":
        if evidence or answer.state_fact_refs or answer.question or answer.unresolved or answer.scope != "formal":
            raise InvalidChange("interaction_contract: product/capability evidence and state claims must use evidence/state, not interaction")
        if turn.applied_scopes or re.search(r"只记录|仅记录|只保存", turn.text):
            raise InvalidChange("interaction_contract: acknowledge applied changes with answer_purpose=state")
        if len(answer.message.strip()) > 600:
            raise InvalidChange("interaction_contract: keep a conversation-only reply within 600 characters")
        # Free prose remains model-dependent. No purported semantic safety gate here.
        return validate_visible_message(answer.message)
    if purpose == "state":
        if evidence:
            raise InvalidChange("state_contract: use evidence for product facts and concepts; state confirmations cannot silently discard them")
        if state_verified:
            return validate_visible_message(render_state_answer(turn, answer))
        if not turn.state():
            raise InvalidChange("state_contract: no saved task; use interaction for a conversation-only reply")
        message = turn._render_state_ack(answer.scope)
        try:
            return validate_visible_message(message)
        except InvalidChange:
            raise InvalidChange("state_contract: no requirements to confirm; use answer_purpose=interaction for a conversation-only reply, or evidence with supported facts") from None
    if answer.state_fact_refs:
        raise InvalidChange("evidence_contract: saved-state answers must use answer_purpose=state")
    if live_verified:
        if answer.answer_boundary:
            raise InvalidChange("answer_boundary cannot bypass live capability answers")
        return validate_visible_message(answer.message)
    from .answer_boundary import render_boundary
    boundary = render_boundary(turn, answer)
    parts = [boundary] if boundary else []
    if limits_requested and turn.state():
        from .delivery import current_limits
        parts.append(current_limits(turn, answer.scope))
    parts.extend(claim_text(turn, c) for c in claims)
    parts.extend(EXPLANATIONS[t] for t in answer.explanation_topics)
    if cups_requested:
        parts.append("仅有 Cups 标注不足以确定升数；必须先确认杯制或同一容量的升数标注，并区分生米、熟饭与容器容量，不能把6 Cups直接当作1.5升。")
    if not parts:
        raise InvalidChange("evidence_contract: provide verified claims or an applicable explanation topic; ordinary conversation uses interaction")
    return validate_visible_message("\n".join(parts))
