"""Pure, evidence-only candidate assessment.

This module deliberately has no database or network dependency.  It computes
relationships, never a single opaque score, and keeps unknown facts distinct
from a poor match.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


STATUSES = {"known", "met", "not_met", "unknown", "conflict", "unsupported"}


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _number(normalized):
    if not isinstance(normalized, dict) or normalized.get("status") != "known":
        return None
    values = normalized.get("values", [])
    if len(values) != 1 or not isinstance(values[0], dict):
        return None
    try:
        value = Decimal(str(values[0]["value"]))
        return value if value.is_finite() else None
    except (KeyError, InvalidOperation, TypeError):
        return None


def numeric_attribute(field, normalized):
    """Canonical numeric fact, independent of whether the user prefers it."""
    from .qualification import NUMERIC, UNITS
    value = _number(normalized)
    if value is None:
        return None
    unit = normalized["values"][0].get("unit")
    if field == "price":
        return {"value": str(value), "unit": "USD"} if unit == "USD" else None
    canonical = NUMERIC.get(field)
    conversion = UNITS.get(str(unit).casefold())
    if canonical and unit == canonical:
        return {"value": str(value), "unit": canonical}
    if canonical and conversion and conversion[0] == canonical:
        return {"value": str(value * Decimal(conversion[1])), "unit": canonical}
    return None


def _match(pref, normalized):
    from .qualification import predicate, NUMERIC
    normalized = normalized if isinstance(normalized, dict) else {}
    refs = [{"field": e.get("field"), "text": e.get("text")} for e in normalized.get("evidence", [])]
    base = {"status": "unknown", "normalized_value": None, "evidence_refs": refs}
    if pref.get("status") != "active":
        return base
    expression = pref.get("expression") or {}
    kind, field = expression.get("kind"), pref.get("field")
    if kind == "qualitative":
        return {**base, "status": "unsupported", "reason": "qualitative preference has no calibrated utility"}
    if normalized.get("status") != "known":
        return {**base, "status": "conflict" if normalized.get("status") == "conflict" else "unknown"}
    numeric = numeric_attribute(field, normalized)
    if kind in {"minimize", "maximize"}:
        if numeric is None:
            return {**base, "status": "unsupported", "reason": "numeric field or unit is not comparable"}
        return {**base, "status": "known", "normalized_value": numeric["value"], "unit": numeric["unit"]}
    target = expression.get("target")
    desired = None
    if kind == "target" and isinstance(target, dict):
        if field == "price":
            if target.get("unit") == "USD" and target.get("operator") in {"eq", "lt", "lte", "gt", "gte"}:
                try:
                    amount = Decimal(target["value"])
                    if amount.is_finite() and amount >= 0:
                        desired = (target["operator"], amount)
                except (InvalidOperation, TypeError):
                    pass
        else:
            desired = predicate(field, target)
    elif kind == "prefer_value":
        desired = predicate(field, target)
    if desired is None:
        return {**base, "status": "unsupported", "reason": "unsupported preference target"}
    op, expected = desired
    if field in NUMERIC or field == "price":
        if numeric is None:
            return {**base, "status": "unsupported", "reason": "target units are not comparable"}
        actual = Decimal(numeric["value"])
        passed = {"eq": actual == expected, "lte": actual <= expected, "lt": actual < expected,
                  "gte": actual >= expected, "gt": actual > expected}.get(op)
        if passed is None:
            return {**base, "status": "unsupported"}
        return {**base, "status": "met" if passed else "not_met",
                "normalized_value": numeric["value"], "unit": numeric["unit"]}
    values = normalized.get("values", [])
    if len(values) != 1 or not isinstance(values[0].get("value"), list):
        return base
    found = set(values[0]["value"])
    passed = not bool(expected & found) if op == "not_contains" else expected <= found
    # Same closed/open vocabulary as hard qualification. Absence in an open
    # source list does not prove either a negative or a failed positive goal.
    closed = field in {"form_factor", "head_type", "tracking", "age_range", "vacuum_type"}
    if not closed and ((op == "not_contains" and passed) or (op != "not_contains" and not passed)):
        return {**base, "reason": "absence_not_proven"}
    return {**base, "status": "met" if passed else "not_met", "normalized_value": sorted(found), "unit": None}


def _hard_status(candidate):
    if not any(k in candidate for k in ("hard_assessment", "qualification", "hard_status")):
        return "unknown"
    hard = candidate.get("hard_status") or candidate.get("hard_assessment") or candidate.get("qualification") or {}
    if isinstance(hard, str):
        return hard
    if hard.get("violated"):
        return "violated"
    if hard.get("unknown") or hard.get("conflict"):
        return "unknown"
    return "satisfied"


def _cmp_for(pref, left, right):
    a, b = left.get("status"), right.get("status")
    if a not in {"known", "met", "not_met"} or b not in {"known", "met", "not_met"}:
        return None
    kind = (pref.get("expression") or {}).get("kind")
    if kind in {"minimize", "maximize"}:
        try: av, bv = Decimal(left["normalized_value"]), Decimal(right["normalized_value"])
        except (KeyError, InvalidOperation, TypeError): return None
        if not av.is_finite() or not bv.is_finite() or left.get("unit") != right.get("unit"):
            return None
        if av == bv: return 0
        better_left = av < bv if kind == "minimize" else av > bv
        return 1 if better_left else -1
    if kind == "prefer_value":
        return 0 if a == b else (1 if a == "met" else -1)
    if kind == "target":
        if a == b: return 0
        return 1 if a == "met" else -1
    return None


def _strict_order(preferences, relations):
    """Return strict preference IDs in high-to-low order only for a complete chain."""
    active = {p["id"]: p for p in preferences if p.get("status") == "active" and p.get("id")}
    edges = [(r["higher_preference_id"], r["lower_preference_id"]) for r in relations
             if r.get("active", True) and r.get("mode") == "strict"]
    if not edges: return []
    if any(a not in active or b not in active or a == b for a,b in edges): return []
    incoming = {x: 0 for x in active}; outgoing = {x: [] for x in active}
    for a,b in edges: outgoing[a].append(b); incoming[b] += 1
    order=[]
    while True:
        roots=[x for x,n in incoming.items() if n==0 and x not in order]
        if len(roots)!=1: break
        node=roots[0]; order.append(node)
        for child in outgoing[node]: incoming[child]-=1
    if len(order) != len(active): return []
    # Independent preference IDs are not a complete chain, so don't invent one.
    return order


def _pairwise(a, b, prefs, relations):
    """Compare two already-qualified candidates and return relation plus reasons."""
    if _hard_status(a) != "satisfied" or _hard_status(b) != "satisfied":
        return {"relation": "incomparable", "product_ids": [a["id"], b["id"]],
                "deciding_dimensions": [], "reason": "hard qualification is not satisfied for both"}
    matches_a, matches_b = a["soft_matches"], b["soft_matches"]
    byid = {p["id"]: p for p in prefs if p.get("status") == "active" and p.get("id")}
    order = _strict_order(prefs, relations)
    if order:
        for pid in order:
            result = _cmp_for(byid[pid], matches_a.get(pid, {}), matches_b.get(pid, {}))
            if result is None:
                return {"relation":"incomparable","product_ids":[a["id"],b["id"]],"deciding_dimensions":[pid],"reason":"higher-priority preference is unknown or unsupported"}
            if result:
                return {"relation":"better" if result>0 else "worse","product_ids":[a["id"],b["id"]],
                        "deciding_dimensions":[pid],"reason":"complete strict preference chain"}
        return {"relation":"equal","product_ids":[a["id"],b["id"]],"deciding_dimensions":order,"reason":"equal on complete strict chain"}
    comparisons=[]
    for pid,pref in byid.items():
        result=_cmp_for(pref,matches_a.get(pid,{}),matches_b.get(pid,{}))
        if result is None: return {"relation":"incomparable","product_ids":[a["id"],b["id"]],"deciding_dimensions":[pid],"reason":"related preference is unknown, conflicting or unsupported"}
        comparisons.append((pid,result))
    if not comparisons: return {"relation":"incomparable","product_ids":[a["id"],b["id"]],"deciding_dimensions":[],"reason":"no active computable soft preference"}
    signs={x[1] for x in comparisons}
    if signs == {0}: relation="equal"
    elif signs <= {0,1} and 1 in signs: relation="better"
    elif signs <= {0,-1} and -1 in signs: relation="worse"
    else: relation="incomparable"
    return {"relation":relation,"product_ids":[a["id"],b["id"]],
            "deciding_dimensions":[x[0] for x in comparisons if x[1]],
            "reason":"conservative comparison across all computable preferences"}


def assess_candidates(task_snapshot, candidate_evidence, request_scope, purpose):
    """Compute an immutable, serializable assessment from trusted evidence."""
    if request_scope not in {"formal", "hypothetical"} or purpose not in {"compare", "recommend"}:
        raise ValueError("invalid assessment scope or purpose")
    if not isinstance(candidate_evidence, list) or not candidate_evidence:
        raise ValueError("candidate evidence is required")
    if any(not isinstance(c, dict) for c in candidate_evidence):
        raise ValueError("candidate evidence must be objects")
    ids=[c.get("id") for c in candidate_evidence]
    excluded = set(task_snapshot.get("excluded", {}))
    if excluded & set(ids):
        raise ValueError("excluded candidate in assessment")
    if any(not isinstance(x,str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("candidate IDs must be unique")
    if task_snapshot.get("scope_ids") is not None and set(ids) - set(task_snapshot["scope_ids"]):
        raise ValueError("candidate outside requested scope")
    requirements=task_snapshot.get("requirements", {})
    prefs=[]
    for key, value in requirements.items():
        if value.get("strength") == "soft" and value.get("status") == "active":
            p=deepcopy(value); p.setdefault("field", key); p.setdefault("id", key); prefs.append(p)
    relations=list(task_snapshot.get("preference_relations", {}).values())
    assessed=[]; gaps=[]
    for candidate in candidate_evidence:
        if not isinstance(candidate,dict) or "id" not in candidate:
            raise ValueError("candidate evidence must contain id")
        hard=_hard_status(candidate)
        normalized=candidate.get("normalized", {})
        matches={p["id"]:_match(p, normalized.get(p.get("field"), {"status":"unknown","values":[],"evidence":[]})) for p in prefs}
        item={"id":candidate["id"],"hard_assessment":deepcopy(candidate.get("hard_assessment", candidate.get("qualification", {}))),
              "hard_status":hard,"soft_matches":matches,"price":deepcopy(candidate.get("price")),
              "evidence_refs":deepcopy(candidate.get("evidence_refs", []))}
        item["numeric_attributes"] = {field: value for field, normalized_value in normalized.items()
                                      if (value := numeric_attribute(field, normalized_value)) is not None}
        assessed.append(item)
        for pid,match in matches.items():
            if match["status"] in {"unknown","conflict","unsupported"}:
                gaps.append({"kind":"product_evidence","product_id":candidate["id"],"preference_id":pid,
                             "status":match["status"],"reason":match.get("reason","missing or unresolved evidence")})
    pairs=[]
    for i,left in enumerate(assessed):
        for right in assessed[i+1:]: pairs.append(_pairwise(left,right,prefs,relations))
    dominates={x["id"]:set() for x in assessed}; dominated_by={x["id"]:set() for x in assessed}
    for pair in pairs:
        if pair["relation"]=="better": dominates[pair["product_ids"][0]].add(pair["product_ids"][1]); dominated_by[pair["product_ids"][1]].add(pair["product_ids"][0])
        elif pair["relation"]=="worse": dominates[pair["product_ids"][1]].add(pair["product_ids"][0]); dominated_by[pair["product_ids"][0]].add(pair["product_ids"][1])
    fronts=[]; remaining=set(ids)
    while remaining:
        front=sorted(x for x in remaining if not (dominated_by[x] & remaining))
        if not front: break
        fronts.append(front); remaining-=set(front)
    by_id = {item["id"]: item for item in assessed}
    decision = task_snapshot.get("decision", {})
    pinned = list(dict.fromkeys(decision.get("focus_ids", []) + decision.get("shortlist_ids", [])))
    emphasis = list(dict.fromkeys(r["higher_preference_id"] for r in relations
                    if r.get("active", True) and r.get("mode") == "emphasis"))
    dimensions = emphasis + [p["id"] for p in prefs if p["id"] not in emphasis]
    prefs_by_id = {p["id"]: p for p in prefs}
    reps, vectors = [], {}
    equivalent_ids = {}
    ordered_items = [by_id[pid] for pid in pinned if pid in by_id] + [i for i in assessed if i["id"] not in pinned]
    for item in ordered_items:
        matches = item["soft_matches"]
        fully_known = item["hard_status"] == "satisfied" and bool(prefs) and all(m["status"] in {"known", "met", "not_met"} for m in matches.values())
        vector = _fingerprint([(pid, matches[pid]["status"], matches[pid].get("normalized_value"), matches[pid].get("unit"))
                               for pid in sorted(matches)]) if fully_known else None
        if vector is not None and vector in vectors and item["id"] not in pinned:
            equivalent_ids[item["id"]] = vectors[vector]
            continue
        if vector is not None:
            vectors.setdefault(vector, item["id"])
        advantages = []
        if item["hard_status"] == "satisfied" and not dominated_by[item["id"]]:
            for pid in dimensions:
                comparisons = [_cmp_for(prefs_by_id[pid], matches[pid], other["soft_matches"][pid])
                               for other in assessed if other["id"] != item["id"] and other["hard_status"] == "satisfied"]
                if 1 in comparisons and -1 not in comparisons:
                    advantages.append(pid)
        if item["id"] in pinned or advantages:
            reps.append({"product_id": item["id"], "dimensions": advantages,
                         "qualification": item["hard_status"],
                         "reason": "user_focus_or_shortlist" if item["id"] in pinned else "known_dimension_advantage",
                         "limitations": [pid for pid, m in matches.items() if m["status"] not in {"known", "met", "not_met"}]})
    eligible = [i for i in assessed if i["hard_status"] == "satisfied"]
    if not reps and len(eligible) == 1:
        reps.append({"product_id": eligible[0]["id"], "dimensions": [], "qualification": "satisfied",
                     "reason": "only_qualified_candidate_not_a_comparative_win", "limitations": []})
    reps.sort(key=lambda r: (0 if r["product_id"] in pinned else 1,
              pinned.index(r["product_id"]) if r["product_id"] in pinned else
              min((dimensions.index(pid) for pid in r["dimensions"]), default=len(dimensions)), r["product_id"]))
    differences = []
    for pair in pairs:
        left, right = [by_id[pid] for pid in pair["product_ids"]]
        rows = []
        for pref in prefs:
            pid = pref["id"]
            cmp = _cmp_for(pref, left["soft_matches"][pid], right["soft_matches"][pid])
            rows.append({"preference_id": pid, "field": pref["field"], "comparison": cmp,
                         "left": deepcopy(left["soft_matches"][pid]), "right": deepcopy(right["soft_matches"][pid])})
        numeric_differences = []
        for field in set(left["numeric_attributes"]) & set(right["numeric_attributes"]):
            a, b = left["numeric_attributes"][field], right["numeric_attributes"][field]
            if a["unit"] == b["unit"]:
                numeric_differences.append({"field": field, "left": a["value"], "right": b["value"],
                    "left_minus_right": str(Decimal(a["value"]) - Decimal(b["value"])), "unit": a["unit"]})
        differences.append({**deepcopy(pair), "dimensions": rows,
            "numeric_differences": sorted(numeric_differences, key=lambda x: x["field"]),
            "left_advantages": [r["preference_id"] for r in rows if r["comparison"] == 1],
            "right_advantages": [r["preference_id"] for r in rows if r["comparison"] == -1],
            "unresolved_dimensions": [r["preference_id"] for r in rows if r["comparison"] is None]})
    coverage=deepcopy(task_snapshot.get("retrieval_coverage", {}))
    coverage.update({"examined_ids":ids,"unknown_qualification":sum(x["hard_status"]=="unknown" for x in assessed)})
    result={"task_id":task_snapshot.get("task_id",task_snapshot.get("id")),"scope":request_scope,
            "requirements_version":task_snapshot.get("requirements_version",0),"evidence_version":task_snapshot.get("candidate_evidence_version",0),
            "scope_fingerprint":_fingerprint({"scope":request_scope,"ids":ids,"requirements":requirements,
                "relations":relations,"context":task_snapshot.get("assessment_context")}),
            "purpose":purpose,"examined_ids":ids,"retrieval_coverage":coverage,
            "assessment_context":deepcopy(task_snapshot.get("assessment_context")),
            "hard_assessments":[{"product_id":x["id"],"status":x["hard_status"],"details":x["hard_assessment"]} for x in assessed],
            "soft_matches":{x["id"]:x["soft_matches"] for x in assessed},"pairwise":pairs,"dominance_fronts":fronts,
            "representatives":reps,"equivalent_ids":equivalent_ids,"comparisons":differences,
            "numeric_attributes":{i["id"]:i["numeric_attributes"] for i in assessed},
            "preferences":deepcopy(prefs),"preference_relations":deepcopy(relations),"emphasis_ids":emphasis,
            "gaps":gaps,"policy_version":"b2-v2"}
    return result
