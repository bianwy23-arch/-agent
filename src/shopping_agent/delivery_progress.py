"""Derived delivery guidance; no state writes, ranking or relaxed evidence gates."""
import re


from .quantity import parse, effective, facts, DISPLAY_LIMIT


def requested_count(text):
    return parse(text)['count']


def effective_count(turn):
    return effective(turn)['count']


def delivery_progress(turn):
    state = turn.state()
    if not state or turn.plan is None:
        return {}
    scopes = turn.active_scopes()
    result = {}
    from .discovery import progress
    for scope in scopes:
        excluded = set(state["excluded"])
        if scope == "hypothetical":
            excluded.update(state["exploration"].get("excluded", {}))
        pool = [pid for pid in state["candidates"] if pid not in excluded and
                (turn.plan.scope_ids is None or pid in turn.plan.scope_ids)]
        eligible = [pid for pid in pool if pid in turn.inspected and
                    not any(turn.qualification(pid, scope)[k] for k in ("violated", "unknown", "conflict"))]
        count = effective_count(turn)
        preferences = [r.get("field", k) for k, r in turn.requirements(scope).items()
                       if r.get("status") == "active" and r.get("strength") == "soft"]
        hypotheses = [h["proposed_soft_expression"]["field"] for h in state.get("hypotheses", {}).values()
                      if h.get("status") == "proposed"]
        from .change_review import change_review
        review = change_review(turn, scope)
        enough = count is not None and len(eligible) >= min(count, DISPLAY_LIMIT)
        expansion = progress(turn, scope)
        result[scope] = {"discovery":expansion,"change_review":review,"requested_count": count, "quantity": facts(turn, scope), "eligible_inspected_ids": eligible,
            "recommend_assessment_ids": pool, "enough_hard_qualified": enough and not expansion,
            "preference_fields": sorted(set(preferences + hypotheses)),
            "next_step": ("已够请求数量的本轮硬条件合格候选。现在用recommend_assessment_ids评估整个已知池，不要逐个查看全池。"
                          "评估后仅为决定性偏好/用户问题的具体缺口补查；无偏好时从已核验项交付即可，不附加未取证属性。"
                          if enough and not expansion else expansion["instruction"] if expansion else "仅补当前请求缺口；整池评估不要求逐个inspect，未知保留未知。"),
            "limits": "这不是排名或finish许可；仍须遵守当前范围、硬条件、偏好、数量、引用和本轮查看门禁。"}
    return result


def claim_repair_evidence(turn, answer):
    """Return exact sources already read, not invented replacements or a repaired answer."""
    products = {}
    ids = list(dict.fromkeys(answer.product_ids + [pid for c in answer.claims for pid in c.product_ids] +
                             [c.product_id for c in answer.citations]))
    for pid in ids:
        inspected = turn.inspected.get(pid)
        if not inspected:
            products[pid] = {"needs_current_inspection": True}
            continue
        from .evidence import issue_evidence
        records = issue_evidence(turn, pid)
        products[pid] = {"evidence_records": records,
                         "attribute_fact_fields": [item["attribute"] for item in records],
                         "claim_sources": {}}
        from .evidence import resolve_source
        from .state import InvalidChange
        for claim in answer.claims:
            if claim.kind != "attribute_fact" or pid not in claim.product_ids:
                continue
            try:
                source = resolve_source(turn, pid, claim.field)
                products[pid]["claim_sources"][claim.key] = {
                    "field": claim.field, "citation": {"product_id": pid, "field": source["source_field"], "quote": source["text"]}}
            except InvalidChange as exc:
                products[pid]["claim_sources"][claim.key] = {"error": str(exc), "next_action": "inspect the relevant source or omit an optional unsupported assertion"}
        price = next((item for item in records if item["attribute"] == "price"), None)
        products[pid]["price_citation"] = ({"product_id": pid, "field": "price", "quote": resolve_source(turn, pid, "price")["text"]} if price else None)
    return {"products": products,
            "instruction": "推荐理由优先复制本轮同商品evidence_records中的evidence_id到evidence_refs，不必重填对应claim和citation。旧格式用claim_sources提供的精确来源修正。一次修正所有缺证claim及失效claim_refs。可选介绍可删除；决定性硬条件、用户询问维度与偏好依据不能删掉绕过验证，须针对该商品字段补证或如实说明限制。"
                           "attribute_fact.field只能用已取证字段；features原文不能直接当成尚未取证的connectivity/form_factor字段。"
                           "价格引用可逐字使用price_citation；不要用额外evidence_gap替换无关介绍，不自动改写选择、需求或商品。"}
