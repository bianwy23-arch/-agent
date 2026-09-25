"""Program constraints on gaps, scene hypotheses and no-progress. Not a planner."""
import re
from copy import deepcopy
from uuid import uuid4


RULES = [
    {"id": "carry-daily-v1", "version": 1, "field": "weight", "scenario":"carrying",
     "patterns": [r"每天携带", r"每天背", r"通勤", r"出门带着", r"每天.{0,12}带.{0,12}出门"],
     "expression": {"kind": "minimize", "target": None, "text": None},
     "reason": "日常携带场景通常更在意重量，这只是可撤回假设。",
     "limitations": ["场景不等于已确认的重量上限或硬条件"]},
    {"id": "stationary-v1", "version": 1, "field": "weight",
     "patterns": [r"固定位置", r"放在桌上", r"不用带着"],
     "expression": {"kind": "qualitative", "target": None, "text": "固定位置使用，重量不一定优先"},
     "reason": "固定位置使用会削弱便携假设。",
     "limitations": ["不能据此排除轻量商品"]},
]


def budget_increase_refused(text):
    return bool(re.search(r'(?:不(?:接受|允许|愿意|想|要|能)?|拒绝|别).{0,3}(?:提高|增加|上调|放宽|加).{0,3}预算|预算.{0,12}(?:不能|不要|不再).{0,3}(?:提高|增加|上调)|预算就这样',text))


def scene_from_text(text):
    # Negation wins when both the old and new scene are mentioned.
    if re.search(r"固定位置|放在桌上|不用带|不用携带|不再携带|不需要携带", text):
        return {"active":False,"text":text}
    if any(re.search(p,text) for p in RULES[0]['patterns']):
        return {"active":True,"text":text}
    return None


def _active_soft(state, field):
    for req in state["requirements"].values():
        if req.get("strength") == "soft" and req.get("field") == field:
            return req
    return None


def propose_from_text(state, text):
    """Return new proposed hypotheses; never writes hard requirements."""
    existing = {(h.get("rule_id"), h.get("proposed_soft_expression", {}).get("kind"))
                for h in state.get("hypotheses", {}).values()}
    proposed = []
    for rule in RULES:
        if not any(re.search(p, text) for p in rule["patterns"]):
            continue
        current = _active_soft(state, rule["field"])
        if current and current.get("status") == "no_preference":
            continue
        if current and current.get("source", {}).get("kind") == "explicit":
            continue
        marker = (rule["id"], rule["expression"]["kind"])
        if marker in existing:
            continue
        hid = str(uuid4())
        proposed.append({
            "id": hid, "scenario_ids": [rule["id"]], "proposed_soft_expression": {
                "field": rule["field"], **deepcopy(rule["expression"])},
            "status": "proposed", "origin": "rule", "rule_id": rule["id"],
            "rule_version": rule["version"], "source_refs": [{"quote": text[:80]}],
            "reason": rule["reason"], "limitations": list(rule["limitations"])})
    return proposed


def classify_gaps(state, assessment, inspected_ids, text=""):
    gaps = []
    budget = state.get("requirements", {}).get("budget")
    pending_fields = sorted({f for p in state.get("pending", {}).values() for f in p["fields"]})
    ambiguous_budget = bool(re.search(r'预算(?:提高|增加|降低|减少|放宽|收紧)(?:一?点|一些)',text) and not re.search(r'预算.{0,10}\d+\s*(?:美元|USD|人民币|元)',text,re.I))
    if ambiguous_budget or "budget" in pending_fields or (budget and budget.get("status") == "active" and
                                      budget.get("value", {}).get("currency") != "USD"):
        gaps.append({"id": "user:budget", "kind": "user_information", "fields": ["budget"],
                     "impact": "pending budget interpretation or unsupported currency blocks the current request",
                     "possible_actions": ["ask_budget"], "status": "open"})
    for field in pending_fields:
        if field != "budget":
            gaps.append({"id": "user:" + field, "kind": "user_information", "fields": [field],
                         "impact": "pending user clarification", "possible_actions": ["ask_user"], "status": "open"})
    if assessment:
        for item in assessment.get("gaps", []):
            pref=next((p for p in assessment.get('preferences',[]) if p['id']==item.get('preference_id')), {})
            field=pref.get('field','unknown')
            gaps.append({**item, 'id':'evidence:'+item['product_id']+':'+field,
                         'kind':'capability' if item.get('status')=='unsupported' else 'product_evidence',
                         'fields':[field], 'evidence_status':item.get('status'),
                         'impact':item.get('reason','unresolved decision evidence'),
                         'raw_source_status':'read' if item['product_id'] in inspected_ids else 'not_read',
                         'possible_actions':['inspect_product','disclose_limit'], 'status':'open'})
        incomparable = [p for p in assessment.get("pairwise", []) if p.get("relation") == "incomparable" and p.get("reason") == "conservative comparison across all computable preferences"]
        if any(c.get('left_advantages') and c.get('right_advantages') and any(set(p['product_ids'])==set(c['product_ids']) for p in incomparable) for c in assessment.get('comparisons',[])):
            gaps.append({"id": "tradeoff:open", "kind": "tradeoff",
                         "impact": "no unique winner under current preferences",
                         "fields":[r.get("field",k) for k,r in state["requirements"].items() if r.get("strength")=="soft" and r.get("status")=="active"], "possible_actions": ["compare", "ask_priority"], "status": "open"})
    known = set((state.get("candidates") or {}).keys())
    gaps.append({"id":"supply:current", "kind":"candidate_supply", "fields":[], "related_ids":sorted(known),
        "impact":"broaden within current hard constraints if the known pool does not answer the request",
        "possible_actions":["search"], "status":"open"})
    if known - set(inspected_ids or []):
        gaps.append({"id": "evidence:uninspected", "kind": "product_evidence",
                     "impact": "known candidates lack current-turn inspection",
                     "possible_actions": ["inspect_product"], "status": "open"})
    if state.get("category") in {"headphones","bluetooth_speakers"} and re.search(r"舒适|音质|续航", text):
        gaps.append({"id": "capability:unsupported-audio", "kind": "capability",
                     "impact": "comfort/sound/battery are not computable in this slice",
                     "fields":[f for f,word in [("comfort","舒适"),("sound","音质"),("battery","续航")] if word in text], "raw_source_status":"read" if set(inspected_ids or []) else "not_read", "possible_actions": ["inspect_product","disclose_limit"], "status": "open"})
    asked = {q.get("question_key") for q in state.get("question_history", [])}
    # Rephrasing or another unrelated requirement update does not reopen a question.
    # A user-originated update on its own fields or new tradeoff evidence may do so.
    signatures = {q.get("question_key"): q.get("basis") for q in state.get("question_history", [])}
    rejected = set(state.get("rejected_directions", []))
    for gap in gaps:
        if gap.get("id") in asked and signatures.get(gap.get("id")) == question_basis(state, gap):
            gap["status"] = "suppressed_repeat"
        if gap.get("id") in rejected or "raise_budget" in rejected and "budget" in str(gap):
            gap["status"] = "rejected_direction"
    for gap in gaps:
        gap.setdefault('fields',[])
        gap.setdefault('related_ids', [gap['product_id']] if gap.get('product_id') else [])
        gap.setdefault('source_refs',[])
        attempt = next((a for a in reversed(state.get('action_history',[])) if a.get('gap_id')==gap['id']),None)
        gap['last_attempt'] = {k:v for k,v in attempt.items() if k!='after_evidence'} if attempt else None
        gap['reopen_condition'] = 'new relevant user information or product evidence; rephrasing alone is insufficient'
        for q in state.get('question_history',[]):
            if q.get('question_key') == gap['id'] and (q.get('response') or {}).get('status') in {'unknown','no_preference','direct_recommendation'}:
                if q.get('basis') == question_basis(state,gap): gap['status']='suppressed_repeat'
    return gaps


def question_basis(state, gap):
    fields = gap.get("fields", [])
    if gap.get("kind") == "tradeoff":
        return {"requirements_version": state["requirements_version"],
                "evidence_version": state["candidate_evidence_version"]}
    return {field: {k: deepcopy(v) for k, v in (state.get("requirements", {}).get(field) or {}).items()
                    if k in {"field", "status", "strength", "value", "expression"}} for field in fields}


def progress_fingerprint(state):
    selection = (state.get("decision") or {}).get("selection")
    return {
        "requirements_version": state.get("requirements_version"),
        "evidence_version": state.get("candidate_evidence_version"),
        "excluded": sorted(state.get("excluded") or {}),
        "shortlist": list((state.get("decision") or {}).get("shortlist_ids") or []),
        "selection": None if not selection else selection.get("product_id"),
    }


def note_progress(state, kind="search"):
    current = progress_fingerprint(state)
    previous = state.setdefault("progress", {})
    if previous.get("fingerprint") == current and kind != "delivery_revalidation":
        previous["repeats"] = previous.get("repeats", 0) + 1
        stagnant = previous["repeats"] >= 2
    else:
        previous["fingerprint"] = current
        previous["repeats"] = 0
        stagnant = False
    return stagnant, previous


def evidence_signature(state):
    """Decision vectors, not IDs/query strings/counter increments, establish progress."""
    import json
    fields = {'price'} | {r.get('field',k) for k,r in state.get('requirements',{}).items() if r.get('status')=='active'}
    fields.discard('budget')
    vectors = set()
    for pid,c in state.get('candidates',{}).items():
        if pid in state.get('excluded',{}): continue
        facts = {k:v for k,v in c.get('facts',{}).items() if k in fields}
        assessment = c.get('assessment') or {}
        if facts or assessment:
            vectors.add(json.dumps({'facts':facts,'qualification':{k:assessment.get(k) for k in ('satisfied','violated','unknown','conflict')}},sort_keys=True,ensure_ascii=False))
    return sorted(vectors)


def record_action(store, task_id, turn_id, intent, before, after, detail):
    with store.db:
        state=store.get(task_id)
        changed = before != after
        if intent['action']=='search' and detail.get('coverage')=='exhaustive_structured_filter':
            known_check=any(a['action']=='search' and a.get('coverage')=='exhaustive_structured_filter' and a['requirements_version']==state['requirements_version'] and a.get('scope','formal')==detail.get('scope','formal') for a in state.get('action_history',[]))
            changed = changed or not known_check
        record={'turn_id':turn_id, **intent, 'requirements_version':state['requirements_version'],
            'evidence_version':state['candidate_evidence_version'], 'new_decision_evidence':changed,
            'after_evidence':after, 'cost_kind':'delivery_revalidation' if intent['action']=='delivery_revalidation' else 'new_evidence', **detail}
        state.setdefault('action_history',[]).append(record)
        store._save(state)
    return record


def compact_state(state, candidate_limit=24):
    if not state: return state
    # Full undo payloads, receipts and evidence remain authoritative in SQLite.
    keep = {k:deepcopy(v) for k,v in state.items() if k not in {'history','receipts','decision_effects','turns','assessments','candidates','action_history'}}
    keep['history']=[{'id':h['id'],'turn_id':h['turn_id'],'quote':h['quote'],
                     'changes':[{'target':w['target'],'key':w['key'],'reverted_by':w.get('reverted_by')} for w in h['writes']]}
                    for h in state.get('history',[])[-8:]]
    keep['known_candidate_ids']=list(state.get('candidates',{}))
    keep['candidates']={pid:{'qualification':c.get('qualification'),'evidence_fields':list(c.get('facts',{}))}
                        for pid,c in list(state.get('candidates',{}).items())[:candidate_limit]}
    keep['action_history']=[{k:deepcopy(v) for k,v in a.items() if k!='after_evidence'} for a in state.get('action_history',[])[-6:]]
    if keep.get('decision'): keep['decision'].pop('events',None)
    if keep.get('exploration'): keep['exploration'].get('decision',{}).pop('events',None)
    keep['context_truncation']={'omitted_candidate_details':max(0,len(keep['known_candidate_ids'])-candidate_limit),
        'evidence_access':'inspect_product accepts any known_candidate_id; no search needed',
        'omitted_fields':['raw evidence','assessment snapshots','receipts','full undo payloads']}
    return keep


def bounded_context(state,messages,user_text,budget,candidate_limit,*,extra=None):
    import json
    summary=compact_state(state,candidate_limit)
    recent=[{k:deepcopy(v) for k,v in m.items() if k in {'role','content','task_id','display_id','product_ids'}} for m in messages]
    result={'current_task':summary,'recent_messages':recent,'CURRENT_USER_INPUT':user_text, **(extra or {})}
    size=lambda: len(json.dumps(result,ensure_ascii=False))
    # Measure each message once; avoid serializing the whole history on every eviction.
    lengths=[len(json.dumps(m,ensure_ascii=False)) for m in recent]
    result['recent_messages']=[]
    base=size()
    original=base+sum(lengths)+max(0,len(lengths)-1)*2
    retained=0;used=base
    for length in reversed(lengths):
        addition=length+(2 if retained else 0)
        if used+addition>budget:
            break
        used+=addition;retained+=1
    omitted=len(recent)-retained
    result['recent_messages']=recent[omitted:]
    recent=result['recent_messages']
    truncated=['old_message']*omitted
    if summary and size()>budget:
        summary['candidates']={};summary['history']=[];truncated.extend(['candidate_details','undo_summary'])
    final_size=size()
    return result, {'configured_chars':budget,'before_chars':original,'after_chars':final_size,
                    'truncated':truncated,'mandatory_overflow':final_size>budget,
                    'messages_total':len(messages),'messages_retained':len(recent),'messages_omitted':len(messages)-len(recent),
                    'budget_scope':'initial_business_context_json_chars; excludes system prompt, tool schemas and within-turn tool exchanges; not provider tokens',
                    'preserved':'current requirements and decisions, task decision summaries, user input, metadata; omitted history is not proof of absence'}


def compact_tool_result(output,limit):
    if not isinstance(output,dict):return output
    result=deepcopy(output)
    if isinstance(result.get('action_outcome'),dict):result['action_outcome'].pop('after_evidence',None)
    if isinstance(result.get('items'),list) and len(result['items'])>limit:
        result['all_returned_ids']=[i['id'] for i in result['items']]
        result['items']=result['items'][:limit]
        result['model_view_truncation']={'omitted_item_details':len(result['all_returned_ids'])-limit,
            'access':'inspect_product with any all_returned_ids; the complete pool is remembered'}
    if len(result.get('comparisons',[]))>limit:
        result['comparisons']=result['comparisons'][:limit]
        result['pairwise']=result.get('pairwise',[])[:limit]
        result['model_view_truncation']={'omitted_pair_details':True,
            'access':'assess_candidates(purpose=compare) on a known subset for pair details; keep full assessment_id for full-pool recommendation',
            'preserved':'examined_ids,hard_assessments,soft_matches,dominance_fronts,representatives,equivalent_ids'}
    return result
