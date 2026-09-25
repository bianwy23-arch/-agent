"""Trusted claim checking and card rendering. Numeric values come from assessment."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from .state import InvalidChange
from .evidence import resolve_source, citation_supports, raw_source


LABELS = {"connectivity": "连接方式", "connection": "连接方式", "form_factor": "佩戴形式",
          "weight": "商品重量", "waterproof": "防水等级", "speaker_type": "音箱类型",
          "price": "历史价格", "budget": "预算", "material":"材料", "number_of_keys":"按键数量", "capacity":"容量", "power":"功率", "head_type":"刀头类型", "age_range":"适用年龄", "power_source":"供电方式", "keyboard_description":"键盘类型", "compatible_devices":"兼容设备", "tracking":"追踪方式", "number_of_buttons":"按钮数量", "wattage":"功率", "voltage":"电压", "vacuum_type":"吸尘器形式", "battery_life":"续航", "light_source":"光源", "shaving_use":"剃须用途"}
COMFORT = re.compile(r"舒适|comfort")
MARKET = re.compile(r"全市场|全网|保证最好|市场最优|库内最优")


def _title(catalog, pid):
    from .presentation import product_name
    return product_name(catalog, pid)


def _match(assessment, pid, pref_id):
    return ((assessment.get("soft_matches") or {}).get(pid) or {}).get(pref_id) or {}


def _pref(requirements, pref_id):
    if not pref_id:
        return None
    for key, value in requirements.items():
        if value.get("id") == pref_id or key == pref_id:
            return value
    return None


def _pair(assessment, a, b):
    for pair in assessment.get("pairwise", []):
        ids = pair.get("product_ids") or []
        if ids == [a, b]:
            return pair
        if ids == [b, a]:
            flipped = {**pair, "product_ids": [a, b]}
            if pair["relation"] == "better":
                flipped["relation"] = "worse"
            elif pair["relation"] == "worse":
                flipped["relation"] = "better"
            return flipped
    return None


def _number(match):
    try:
        value = Decimal(str(match["normalized_value"]))
        return value if value.is_finite() else None
    except (KeyError, InvalidOperation, TypeError):
        return None


def display_number(value):
    amount = Decimal(str(value))
    rounded = amount.quantize(Decimal("0.001")) if abs(amount) < Decimal("1e20") else amount
    text = format(rounded, "f").rstrip("0").rstrip(".") if "." in format(rounded, "f") else format(rounded, "f")
    return ("约 " if rounded != amount else "") + text


def verify_claims(turn, answer, assessment):
    """Return program-authored claims. Model supplies kind/ids, never deltas."""
    if answer.kind == "answered" and not answer.claims and MARKET.search(answer.message or ""):
        raise InvalidChange("cannot claim a market-wide or catalog-wide optimum")
    if answer.kind == "answered" and answer.product_ids and not answer.claims:
        raise InvalidChange("product answers require verified claims; use attribute_fact or comparative claims")
    if answer.claims and not turn.task_id:
        raise InvalidChange("product claims require an active task")
    requirements = turn.requirements(answer.scope) if turn.task_id else {}
    verified = []
    for claim in answer.claims:
        if claim.kind not in {"attribute_fact", "evidence_gap"} and (COMFORT.search(claim.key) or (claim.field and COMFORT.search(claim.field))):
            raise InvalidChange("comfort cannot be inferred from weight or other unrelated fields")
        pids = claim.product_ids
        if len(set(pids)) != len(pids):
            raise InvalidChange("claim products must be distinct")
        if claim.conditions:
            raise InvalidChange("free-text claim conditions are not verified; omit them")
        if turn.plan.scope_ids is not None and set(pids) - set(turn.plan.scope_ids):
            raise InvalidChange("claim outside user scope")
        if set(pids) & set(turn.state()["excluded"]):
            raise InvalidChange("claim refers to an excluded product")
        if assessment and set(pids) - set(assessment["examined_ids"]):
            raise InvalidChange("claim outside assessment")
        if any(pid not in turn.state()["candidates"] for pid in pids):
            raise InvalidChange("claim refers to an unknown product")
        if claim.kind == "attribute_fact":
            if len(pids) != 1 or not claim.field:
                raise InvalidChange("attribute_fact requires one product and a field")
            inspected = turn.inspected.get(pids[0])
            if not inspected:
                raise InvalidChange("attribute_fact requires current-turn inspection")
            try:
                source = resolve_source(turn, pids[0], claim.field)
            except InvalidChange as exc:
                raise InvalidChange("claim " + claim.key + " for " + pids[0] + " field " + claim.field + ": attribute_fact has no source evidence. " + str(exc)) from None
            text = source["text"]
            excerpts = [citation.quote for citation in answer.citations
                        if citation_supports(turn, citation, source)]
            if excerpts and isinstance(raw_source(inspected, claim.field), list):
                text = '；'.join(dict.fromkeys(excerpts))
            verified.append({"key": claim.key, "kind": claim.kind, "product_ids": pids, "field": claim.field,
                             "text": f"{_title(turn.catalog, pids[0])} 的资料标注“{text}”。",
                             "numeric_value": None, "conditions": list(claim.conditions), "source": source})
            continue
        if claim.kind == "evidence_gap":
            from .answer_boundary import evidence_status, field_label
            if not claim.field:
                raise InvalidChange("evidence_gap requires a field")
            label = field_label(claim.field)
            for pid in pids:
                status = evidence_status(turn, pid, claim.field)
                if status not in {"missing", "conflict"}:
                    raise InvalidChange("evidence_gap requires an actual unresolved dimension with checked missing/conflicting evidence; status=" + status)
                wording = "资料存在冲突，暂不能据此判断" if status == "conflict" else "本轮已查看资料，但没有可核验的对应信息，暂不能判断"
                verified.append({"key":claim.key,"kind":claim.kind,"product_ids":[pid],"field":claim.field,
                    "numeric_value":None,"conditions":[],"text":f"{_title(turn.catalog,pid)}的{label}：{wording}；这不代表商品不满足，也不代表其他来源没有证据。"})
            continue
        if assessment is None:
            raise InvalidChange("comparative claims require a current assessment")
        if claim.kind == "numeric_difference":
            if len(pids) != 2 or not claim.field:
                raise InvalidChange("numeric_difference requires two products and a field")
            field = claim.field
            pref = _pref(requirements, claim.preference_id) if claim.preference_id else next(
                (r for r in requirements.values() if r.get("field") == field and
                 r.get("strength") == "soft" and r.get("status") == "active"), None)
            if claim.preference_id and (not pref or pref.get("field") != field or pref.get("status") != "active"):
                raise InvalidChange("numeric_difference field must match an active preference")
            if pref:
                matches = [_match(assessment, pid, pref["id"]) for pid in pids]
            else:
                matches = [{"status": "known", "normalized_value": fact.get("value"), "unit": fact.get("unit")}
                           if (fact := assessment.get("numeric_attributes", {}).get(pid, {}).get(field)) else {}
                           for pid in pids]
            from .qualification import NUMERIC
            expected_unit = "USD" if field == "price" else NUMERIC.get(field)
            if not expected_unit or any(m.get("status") not in {"known", "met", "not_met"}
                                       or m.get("unit") != expected_unit for m in matches):
                raise InvalidChange("numeric_difference requires comparable known values and units")
            left, right = [_number(m) for m in matches]
            if left is None or right is None:
                raise InvalidChange("numeric_difference requires finite values")
            unit = expected_unit
            delta = left - right
            if delta == 0:
                verified.append({"key":claim.key,"kind":claim.kind,"product_ids":pids,"field":field,
                    "numeric_value":"0","unit":unit,
                    "text":f"{_title(turn.catalog,pids[0])} 与 {_title(turn.catalog,pids[1])} 的{LABELS.get(field,field)}相同，差值为 0 {unit}。",
                    "conditions":list(claim.conditions)})
                continue
            unit = unit or ("USD" if field == "price" else "")
            if field == "weight":
                direction = "更重" if delta > 0 else "更轻"
            elif field == "price":
                direction = "更贵" if delta > 0 else "更便宜"
            else:
                direction = "数值更高" if delta > 0 else "数值更低"
            verified.append({"key": claim.key, "kind": claim.kind, "product_ids": pids, "field": field,
                             "numeric_value": str(abs(delta)), "unit": unit,
                             "text": f"{_title(turn.catalog, pids[0])} 比 {_title(turn.catalog, pids[1])} {direction} {display_number(abs(delta))} {unit}。".strip(),
                             "conditions": list(claim.conditions)})
            continue
        if claim.kind == "preference_advantage":
            if len(pids) != 2 or not claim.preference_id:
                raise InvalidChange("preference_advantage requires two products and a preference")
            pref = _pref(requirements, claim.preference_id)
            from .assessment import _cmp_for
            if not pref or pref.get("status") != "active" or _cmp_for(
                    pref, _match(assessment, pids[0], pref["id"]),
                    _match(assessment, pids[1], pref["id"])) != 1:
                raise InvalidChange("preference_advantage is not supported on the named dimension")
            label = LABELS.get((pref or {}).get("field"), (pref or {}).get("field") or claim.preference_id)
            if pref.get("expression", {}).get("kind") in {"target", "prefer_value"}:
                text = f"按已记录的{label}软目标，{_title(turn.catalog,pids[0])}满足目标，{_title(turn.catalog,pids[1])}不满足目标；这不是硬筛选条件。"
            else:
                text = f"按已记录的{label}偏好，{_title(turn.catalog,pids[0])}优于{_title(turn.catalog,pids[1])}。"
            verified.append({"key": claim.key, "kind": claim.kind, "product_ids": pids,
                             "preference_id": claim.preference_id, "numeric_value": None,
                             "text": text,
                             "conditions": list(claim.conditions)})
            continue
        if claim.kind == "tradeoff":
            if len(pids) != 2:
                raise InvalidChange("tradeoff requires two products")
            pair = _pair(assessment, pids[0], pids[1])
            from .assessment import _cmp_for
            signs = {_cmp_for(r, _match(assessment, pids[0], r["id"]),
                              _match(assessment, pids[1], r["id"]))
                     for r in requirements.values() if r.get("strength") == "soft" and r.get("status") == "active"}
            if not pair or pair["relation"] != "incomparable" or not {1, -1} <= signs:
                raise InvalidChange("tradeoff requires known advantages in both directions, not merely unknown values")
            verified.append({"key": claim.key, "kind": claim.kind, "product_ids": pids, "numeric_value": None,
                             "text": f"{_title(turn.catalog, pids[0])} 与 {_title(turn.catalog, pids[1])} 在已记录软偏好上互有优劣，不能伪造唯一最优。",
                             "conditions": list(claim.conditions)})
            continue
        if claim.kind == "conditional_recommendation" and claim.hypothesis_id:
            hyp = (turn.state().get('hypotheses') or {}).get(claim.hypothesis_id)
            if not hyp or hyp['status'] != 'proposed' or hyp != assessment.get('hypotheses',{}).get(claim.hypothesis_id):
                raise InvalidChange('conditional hypothesis must be current and proposed')
            expression = hyp['proposed_soft_expression']
            pref = {'field':expression['field'],'expression':{k:v for k,v in expression.items() if k!='field'}}
            from .assessment import _cmp_for
            matches = assessment.get('hypothesis_matches',{})
            if len(pids)!=2 or _cmp_for(pref,matches.get(pids[0],{}).get(claim.hypothesis_id,{}),matches.get(pids[1],{}).get(claim.hypothesis_id,{})) != 1:
                raise InvalidChange('claim '+claim.key+': hypothesis '+claim.hypothesis_id+' is for '+pref['field']+' only; this product order has no known advantage on that field. Remove this unsupported claim or use an actual active preference/hypothesis for the intended field. A price alternative cannot cite a weight hypothesis.')
            verified.append({'key':claim.key,'kind':claim.kind,'product_ids':pids,'hypothesis_id':claim.hypothesis_id,
                'numeric_value':None,'conditions':['场景假设尚未确认，可随时撤回'],
                'text':f"如果按你提到的场景暂时更看重{LABELS.get(pref['field'],pref['field'])}，可优先考虑{_title(turn.catalog,pids[0])}。这是尚未确认、可撤回的场景假设，不是你的明确要求。" +
                       ' 此维度有已知优势，不代表更舒适或其他条件全部满足。'})
            continue
        if claim.kind == "conditional_recommendation":
            pref = _pref(requirements, claim.preference_id)
            from .assessment import _cmp_for
            if len(pids) != 2 or not pref or pref.get("status") != "active" or _cmp_for(
                    pref, _match(assessment, pids[0], pref["id"]), _match(assessment, pids[1], pref["id"])) != 1:
                raise InvalidChange("claim "+claim.key+": conditional recommendation requires a known preference advantage and an active preference_id. If no preference exists for this field, remove this claim and retain only the factual numeric_difference.")
            label = LABELS.get(pref["field"], pref["field"])
            verified.append({"key": claim.key, "kind": claim.kind, "product_ids": pids,
                             "preference_id": pref["id"], "numeric_value": None, "conditions": [],
                             "text": f"如果当前更看重{label}这一项偏好，可优先考虑{_title(turn.catalog, pids[0])}；"
                                     "这只是该维度的条件性建议，不证明其他条件全部满足。"})
            continue
        raise InvalidChange("unsupported claim kind")
    return verified


def verify_proposal(turn, answer, assessment):
    proposal = answer.recommendation_proposal
    if proposal is None:
        if answer.kind == "recommendation" and assessment is not None:
            raise InvalidChange("assessed recommendation requires recommendation_proposal")
        return None
    if assessment is None or proposal.assessment_id != assessment["assessment_id"]:
        raise InvalidChange("recommendation_proposal requires a current assessment")
    examined = set(assessment["examined_ids"])
    excluded = set(turn.state()["excluded"])
    if set(proposal.ordered_ids) - examined:
        raise InvalidChange("proposal contains products outside the assessment")
    if set(proposal.ordered_ids) & excluded:
        raise InvalidChange("excluded products cannot be main recommendations")
    if answer.product_ids != proposal.ordered_ids:
        raise InvalidChange("product_ids must match proposal order")
    claim_keys = {c.key for c in answer.claims}
    if set(proposal.claim_refs) - claim_keys:
        raise InvalidChange("proposal claim_refs must exist in claims")
    eligible = {item["product_id"] for item in assessment["hard_assessments"] if item["status"] == "satisfied"}
    if answer.kind == "recommendation":
        if any(pid not in eligible for pid in proposal.ordered_ids):
            raise InvalidChange("main recommendation must pass current hard qualification")
    if len(set(proposal.ordered_ids)) != len(proposal.ordered_ids):
        raise InvalidChange("proposal order must contain unique products")
    for pair in assessment.get("pairwise", []):
        if pair.get("reason") != "complete strict preference chain" or pair["relation"] not in {"better", "worse"}:
            continue
        winner, loser = pair["product_ids"] if pair["relation"] == "better" else pair["product_ids"][::-1]
        if loser in proposal.ordered_ids and (winner not in proposal.ordered_ids or
                proposal.ordered_ids.index(winner) > proposal.ordered_ids.index(loser)):
            raise InvalidChange("proposal violates a complete strict preference chain")
    from .assessment import _cmp_for, _strict_order
    prefs = {p["id"]: p for p in assessment.get("preferences", [])}
    if set(proposal.preference_refs) - set(prefs):
        raise InvalidChange("proposal.preference_refs must use active preference IDs (not product IDs or field names): " + str({p["field"]:pid for pid,p in prefs.items()}))
    lead = proposal.ordered_ids[0]
    rivals = sorted(eligible - {lead})
    for pid in assessment.get("emphasis_ids", []):
        reason = proposal.emphasis_reasons.get(pid)
        if pid not in proposal.preference_refs or reason is None:
            raise InvalidChange("proposal.emphasis_reasons must use preference ID as KEY, not product ID. Required key="+pid+" field="+prefs[pid]["field"]+"; choose an evidence-supported reason advantage/tradeoff/equal/unknown/strict and include this ID in preference_refs.")
        comparisons = {other: _cmp_for(prefs[pid], _match(assessment, lead, pid), _match(assessment, other, pid)) for other in rivals}
        values = list(comparisons.values())
        valid = (reason == "advantage" and 1 in values and -1 not in values or
                 reason == "equal" and all(v == 0 for v in values) or
                 reason == "unknown" and None in values or
                 reason == "strict" and bool(_strict_order(list(prefs.values()), assessment.get("preference_relations", []))))
        if reason == "tradeoff":
            valid = any(cmp == -1 and any(_cmp_for(pref, _match(assessment, lead, key), _match(assessment, other, key)) == 1
                        for key, pref in prefs.items() if key != pid) for other, cmp in comparisons.items())
        if not valid:
            raise InvalidChange("emphasis reason is not supported by current evidence")
    if set(proposal.emphasis_reasons) - set(assessment.get("emphasis_ids", [])):
        raise InvalidChange("emphasis reason must reference a current emphasis relation: valid emphasis_reasons KEYS are "+str(assessment.get("emphasis_ids", []))+"; values are advantage/tradeoff/equal/unknown/strict. Remove product-ID keys.")
    rep_by_id = {r["product_id"]: r for r in assessment.get("representatives", [])}
    for product_id, role in proposal.representative_roles.items():
        if product_id not in proposal.ordered_ids or product_id not in rep_by_id:
            raise InvalidChange("representative role must reference a displayed program representative")
        if role not in rep_by_id[product_id]["dimensions"] and role != rep_by_id[product_id]["reason"]:
            raise InvalidChange("representative role must be an actual dimension or program reason")
    for hid in proposal.hypothesis_refs:
        hyp = turn.state().get('hypotheses',{}).get(hid)
        if not hyp or hyp['status'] != 'proposed' or not any(c.key in proposal.claim_refs and c.hypothesis_id==hid
                and c.kind=='conditional_recommendation' and c.product_ids[0]==lead for c in answer.claims):
            raise InvalidChange('proposal hypothesis must have a current conditional claim for its lead')
    result = proposal.model_dump(mode="json")
    result["verified_emphasis"] = [{"preference_id": pid, "field": prefs[pid]["field"], "reason": reason,
                                    "lead_id": lead} for pid, reason in proposal.emphasis_reasons.items()]
    return result


def product_cards(turn, product_ids, display_id, scope="formal", relevant_fields=None):
    state = turn.state()
    decision = state["exploration"]["decision"] if scope == "hypothetical" else state["decision"]
    requirements = turn.requirements(scope)
    relevant = set(relevant_fields or []) | {r.get("field",key) for key,r in requirements.items() if r.get("status")=="active"}
    cards = []
    for pid in product_ids:
        inspected = turn.inspected.get(pid) or {}
        facts = inspected.get("facts") or state["candidates"].get(pid, {}).get("facts") or {}
        price = facts.get("price", {}).get("value") or state["candidates"].get(pid, {}).get("facts", {}).get("price", {}).get("value")
        hard = turn.catalog.qualification(pid, requirements)
        qualification = "violated" if hard["violated"] else "unknown" if hard["unknown"] or hard["conflict"] else "satisfied"
        attrs = []
        for field, fact in facts.items():
            if field in {"price", "budget"} or (relevant and field not in relevant):
                continue
            evidence = fact.get("evidence") or {}
            text = evidence.get("text") if isinstance(evidence, dict) else None
            attrs.append({"field": field, "label": LABELS.get(field, "商品信息"),
                          "status": fact.get("status"), "text": text, "unknown": fact.get("status") == "unknown",
                          "evidence": deepcopy(evidence)})
        cards.append({
            "product_id": pid,
            "title": turn.catalog._products[pid]["title"],
            "display_name": _title(turn.catalog, pid),
            "price": deepcopy(price),
            "qualification": qualification,
            "shortlisted": pid in decision["shortlist_ids"],
            "excluded": pid in state["excluded"],
            "selected": (decision.get("selection") or {}).get("product_id") == pid,
            "display_id": display_id, "scope": scope,
            "attributes": attrs,
            "evidence_fields": [a["field"] for a in attrs if a.get("text")],
        })
    return cards


def comparison_view(turn, assessment, verified_claims, product_ids, display_id):
    if assessment is None:
        return None
    gaps = [{"kind": g.get("kind"), "product_id": g.get("product_id"), "status": g.get("status"),
             "reason": g.get("reason")} for g in assessment.get("gaps", [])]
    prefs = {p["id"]: p for p in assessment.get("preferences", [])}
    fields = {c["field"] for c in verified_claims if c.get("field")}
    fields.update(prefs[c["preference_id"]]["field"] for c in verified_claims if c.get("preference_id") in prefs)
    if not fields:
        fields = {p["field"] for p in prefs.values()}
    requirements = turn.requirements(assessment["scope"])
    fields = {requirements.get(field, {}).get("field") or field for field in fields}
    # Identity/source text is evidence, not a comparison dimension.
    fields -= {"title", "product_id", "id", "features", "description"}
    from .presentation import customer_number
    product_ids = product_ids or assessment["examined_ids"]
    rows = []
    for field in sorted(fields):
        cells = []
        for pid in product_ids:
            numeric = assessment.get("numeric_attributes", {}).get(pid, {}).get(field)
            if numeric:
                value = customer_number(numeric["value"], numeric["unit"])
                status = "known"
            else:
                match = next((assessment["soft_matches"][pid].get(p["id"], {}) for p in prefs.values() if p["field"] == field), {})
                status = match.get("status", "unknown")
                value = "、".join(map(str, match["normalized_value"])) if isinstance(match.get("normalized_value"), list) else status
            cells.append({"product_id":pid,"text":value,"status":status})
        rows.append({"field":field,"label":LABELS.get(field,"其他条件"),"cells":cells})
    return {
        "assessment_id": assessment["assessment_id"],
        "purpose": assessment.get("purpose"), "scope": assessment["scope"], "rows": rows,
        "pairwise": deepcopy(assessment.get("pairwise", [])),
        "dominance_fronts": deepcopy(assessment.get("dominance_fronts", [])),
        "representatives": deepcopy(assessment.get("representatives", [])),
        "claims": verified_claims,
        "gaps": gaps,
        "products": product_cards(turn, product_ids or assessment["examined_ids"], display_id, assessment["scope"], fields),
    }


def feedback(state, answer, assessment):
    notes = []
    decision = state['exploration']['decision'] if answer.scope=='hypothetical' and state.get('exploration') else state['decision']
    def violated(pid):
        if answer.scope=='hypothetical':
            return bool((state.get('exploration') or {}).get('qualifications',{}).get(pid,{}).get('violated'))
        return state['candidates'].get(pid,{}).get('qualification')=='violated'
    selection = decision.get("selection")
    if selection and selection.get("validity") == "needs_review":
        notes.append("已记录您的选择，但当前资格需要复核，不能当作已合格或已购买。")
    if selection and selection.get("commitment") == "confirmed" and selection.get("validity") != "valid":
        notes.append("明确选择仍然保留；资格变化不会偷偷改成另一件商品。")
    over = [pid for pid in decision["shortlist_ids"] if violated(pid)]
    if over:
        notes.append("保留项中包含当前硬条件不合格的商品，可继续比较，但不能作为主推荐。")
    mentioned = set(answer.product_ids) | {pid for c in answer.claims for pid in c.product_ids}
    if mentioned:
        invalid = [pid for pid in mentioned if violated(pid)]
        if invalid:
            notes.append('当前展示中有商品违反已记录硬条件，仅作比较参考，不能作为主推荐。')
    if assessment:
        unknown = [g for g in assessment.get("gaps", []) if g.get("status") in {"unknown", "conflict", "unsupported"}]
        if unknown:
            notes.append("部分软偏好缺少可比较证据，未知或冲突不会被当成零分。")
        if answer.kind == "recommendation" and assessment.get("representatives") and len(answer.product_ids) < len(assessment["representatives"]):
            notes.append("展示数量按当前请求给出，不足时不会凑商品。")
    return notes


def current_limits(turn,scope):
    """Report live business limits from authority, not generic explanatory prose."""
    state=turn.state();requirements=turn.requirements(scope);notes=[]
    if state.get('pending'):
        notes.append('还有部分要求需要你确认，确认前沿用原来的条件。')
    budget=requirements.get('budget',{})
    if budget.get('status')=='active':
        value=budget['value'];notes.append(f"当前预算上限为 {value['amount']} {value['currency']}。")
        if value['currency']!='USD':notes.append('这里的参考价以美元计，暂不能直接与这个币种的预算比较。')
    ids=turn.plan.scope_ids if turn.plan.scope_ids is not None else list(state['candidates'])
    checked=[turn.catalog.qualification(pid,requirements) for pid in ids if pid not in state['excluded']]
    violated=sum(bool(c['violated']) for c in checked)
    unknown=sum(not c['violated'] and bool(c['unknown'] or c['conflict']) for c in checked)
    if checked:
        notes.append(f'目前看过的 {len(checked)} 款中，{violated} 款不符合要求，{unknown} 款还缺少足够资料。')
        if violated+unknown==len(checked):notes.append('这些商品目前还不能推荐为符合你全部要求。')
    else:notes.append('还没有足够商品资料判断是否符合你的要求。')
    if 'raise_budget' in state.get('rejected_directions',[]):notes.append('已记录不接受提高预算，本轮不重提或追问这条方向。')
    from .action_policy import classify_gaps
    for gap in classify_gaps(state,None,list(turn.inspected),turn.text):
        if gap['kind']=='capability':
            fields='、'.join({'battery':'耳机续航','sound':'音质','comfort':'舒适度'}.get(f,f) for f in gap['fields'])
            notes.append('目前还无法把'+fields+'作为可靠的比较依据，需要更多资料或实测。')
    notes.append('这些限制只针对目前掌握的资料；其他商品仍可能合适。')
    return '\n'.join(notes)


def render_evidence_limit(turn, answer, verified_claims):
    """Explain an evidence limit without publishing the model's unverified draft.

    Numeric comparison support comes from the existing category/unit contract;
    it is not a promise of data availability, preference or an overall ranking.
    """
    realtime = any(word in turn.text for word in ["实时", "库存", "当前报价", "有货", "现在的售价"])
    reviews = any(word in turn.text for word in ["评论", "抱怨", "评价原文"])
    if realtime:
        return "我查不到实时售价和库存，需要以销售页面为准。"
    if reviews:
        return "我没有用户评论原文或统计，无法判断大家最常抱怨什么。"
    return "现有资料不足以判断你问的哪款最好或实测表现；商品标注不能代替实际体验和测试。"


def validate_recommendation_reasons(turn, answer, verified_claims):
    """Validate provenance only. Free-text relevance/entailment needs semantic eval."""
    if not answer.recommendation_reasons:
        return
    if answer.kind != 'recommendation':
        raise InvalidChange('recommendation_reasons only belong to recommendation answers')
    claims = {c['key']: c for c in verified_claims}
    counts = {}
    seen = set()
    for reason in answer.recommendation_reasons:
        if reason.product_id not in answer.product_ids:
            raise InvalidChange('reason product must be a current main recommendation')
        counts[reason.product_id] = counts.get(reason.product_id, 0) + 1
        if counts[reason.product_id] > 2:
            raise InvalidChange('at most two short reasons per recommended product')
        if not reason.text.strip() or (reason.product_id, reason.text.strip()) in seen:
            raise InvalidChange('reason text must be nonempty and distinct for its product')
        seen.add((reason.product_id, reason.text.strip()))
        if len(set(reason.claim_refs)) != len(reason.claim_refs):
            raise InvalidChange('reason claim_refs must be distinct')
        for ref in reason.claim_refs:
            claim = claims.get(ref)
            if claim is None or reason.product_id not in claim['product_ids']:
                raise InvalidChange('reason reference must identify a verified claim for the same product: '+ref)
            if claim['kind'] == 'attribute_fact':
                source = claim["source"]
                if not any(citation_supports(turn, c, source) for c in answer.citations):
                    raise InvalidChange('attribute reason requires an exact source citation for its product and field: '+ref)
