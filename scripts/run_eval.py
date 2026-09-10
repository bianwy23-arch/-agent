"""Real-provider task replay. Gold is used only after all model calls in a task.

No expected IDs, predicates or tool sequence is passed to ShoppingRuntime.
Semantic rubric remains a separate review and cannot be auto-passed here.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from shopping_agent.config import Settings
from shopping_agent.runtime import ShoppingRuntime


def equivalent(actual, wanted):
    if isinstance(actual,str):
        aliases={'儿童适用':'kid','儿童':'kid','children':'kid','child':'kid','kid':'kid','成人':'adult','LED':'led','蓝牙':'bluetooth','无线':'wireless','有线':'wired'}
        actual={'operator':'contains','value':aliases.get(actual,actual),'unit':None}
    if not isinstance(actual,dict):return False
    # Equivalent numeric units and eq/contains enum spelling are not task errors.
    if wanted['unit']:
        units={'l':Decimal(1),'ml':Decimal('.001'),'g':Decimal(1),'kg':Decimal(1000),'w':Decimal(1),'count':Decimal(1)}
        try:return actual['operator']==wanted['operator'] and Decimal(actual['value'])*units[actual['unit'].casefold()]==Decimal(wanted['value'])*units[wanted['unit'].casefold()]
        except (KeyError,ValueError):return False
    return actual.get('operator') in {'contains','eq'} and wanted['operator'] in {'contains','eq'} and actual.get('unit') is None and str(actual.get('value')).casefold()==str(wanted['value']).casefold()


def check_turn(output, expected, outputs, events, conversation, products):
    failures=[]
    def require(ok, code):
        if not ok:failures.append(code)
    state=output.get('state') or {}; reqs=state.get('requirements',{}); ids=output['product_ids']
    tools=[e for e in events if e['event']=='tool']
    require(output['runtime']['error'] is None,'runtime_failure')
    require(output['kind'] in expected['kind'],'wrong_stop_kind')
    require(set(ids)<=products.keys(),'unknown_product_id')
    if 'category' in expected:require(state.get('category')==expected['category'],'category')
    if 'budget' in expected:
        budget=reqs.get('budget',{}).get('value',{}) or {}
        try:correct=Decimal(budget.get('amount','NaN'))==Decimal(expected['budget'])
        except Exception:correct=False
        require(correct,'budget_state')
        require(budget.get('currency')==expected.get('currency','USD'),'budget_currency')
        if output['kind']=='recommendation':require(all(Decimal(str(products[p]['price']['amount']))<=Decimal(expected['budget']) for p in ids),'budget_violation')
    for field,wanted in expected.get('hard',{}).items():
        r=reqs.get(field,{}); actual=r.get('value')
        require(r.get('strength')=='hard' and r.get('status')=='active','missing_hard_'+field)
        # Explicit schema contract avoids interpreting a second free-form sentence as gold.
        require(equivalent(actual,wanted),'predicate_'+field)
    if 'allowed_ids' in expected:require(set(ids)<=set(expected['allowed_ids']),'gold_attribute_violation')
    require(len(ids)>=expected.get('min_products',0),'incomplete_product_count')
    require(len(ids)<=expected.get('max_products',100),'excess_product_count')
    if expected.get('no_search'):require(not any(e['name']=='search_products' for e in tools),'unnecessary_search')
    if expected.get('no_inspect'):require(not any(e['name']=='inspect_product' for e in tools),'inspect_after_stop')
    if expected.get('exhaustive'):require(any(e['name']=='search_products' and e['result'].get('coverage')=='exhaustive_structured_filter' for e in tools),'missing_exhaustive_search')
    if expected.get('cheapest') and ids:
        cheapest=min(p['price']['amount'] for p in products.values() if p['category_id']==expected['category'])
        require(all(products[p]['price']['amount']==cheapest for p in ids),'not_catalog_cheapest')
    if 'excluded_display' in expected:
        index,position=expected['excluded_display']; shown=outputs[index]['product_ids']
        require(len(shown)>position and shown[position] in state.get('excluded',{}),'wrong_exclusion_reference')
    if expected.get('excluded_empty'):require(not state.get('excluded'),'exclusion_not_undone')
    if expected.get('facts_survive'):
        prior=(outputs[0].get('state') or {}).get('candidates',{})
        require(bool(outputs[0].get('state')),'prior_state_unavailable')
        require(all(state.get('candidates',{}).get(pid,{}).get('facts')==p['facts'] for pid,p in prior.items()),'facts_lost')
    if 'pending' in expected:require(all(any(f in x['fields'] for x in state.get('pending',{}).values()) for f in expected['pending']),'pending_missing')
    if expected.get('pending_empty'):require(not state.get('pending'),'pending_not_cleared')
    if 'exploration_budget' in expected:
        ex=state.get('exploration') or {}; b=ex.get('overrides',{}).get('budget',{}).get('value',{})
        require(b.get('amount')==expected['exploration_budget'],'hypothesis_state')
    if expected.get('exploration_none'):require(state.get('exploration') is None,'hypothesis_not_ended')
    if 'same_task_as' in expected:require(output['task_id']==outputs[expected['same_task_as']]['task_id'],'wrong_resumed_task')
    if 'task_count' in expected:require(len(conversation['task_ids'])==expected['task_count'],'task_count')
    if 'scope_display' in expected:require(set(ids)==set(outputs[expected['scope_display']]['product_ids']),'comparison_scope')
    if 'no_preference' in expected:require(reqs.get(expected['no_preference'],{}).get('status')=='no_preference','not_explicit_no_preference')
    if 'absent' in expected:require(expected['absent'] not in reqs or reqs[expected['absent']]['status']=='no_preference','revoked_requirement_resurrected')
    if expected.get('unknown_hard'):require(any(k!='budget' and r['status']=='active' and r['strength']=='hard' for k,r in reqs.items()),'unknown_hard_dropped')
    if expected.get('unsupported_category'):require(not output['task_id'],'unsupported_task_created')
    if expected.get('budget_unresolved'):require(not reqs.get('budget') or any('budget' in p['fields'] for p in state.get('pending',{}).values()),'conflict_silently_resolved')
    # All returned recommendations must have actual citations; semantic entailment separately reviewed.
    if output['kind']=='recommendation':require(set(ids)<=set(c['product_id'] for c in output['citations']),'missing_citation')
    return failures


async def main(args):
    spec=json.loads((args.suite/'cases.json').read_text()); selected=[c for c in spec['cases'] if args.split=='all' or c['split']==args.split]
    if args.ids:selected=[c for c in selected if c['id'] in args.ids.split(',')]
    if not selected:raise ValueError('No selected cases')
    args.output.mkdir(parents=True,exist_ok=False)
    gold=json.loads((args.suite/'gold.json').read_text())
    cases_hash=hashlib.sha256((args.suite/'cases.json').read_bytes()).hexdigest()
    gold_hash=hashlib.sha256((args.suite/'gold.json').read_bytes()).hexdigest()
    source_root=Path(__file__).resolve().parents[1]
    source_hashes={str(p.relative_to(source_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_root.glob('src/shopping_agent/*.py')}
    source_hashes['scripts/run_eval.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (args.output/'provenance.json').write_text(json.dumps({'cases':cases_hash,'gold':gold_hash,'source':source_hashes},indent=2)+'\n')
    manifest=args.root/'data/amazon/catalog_v1/manifest.json'
    if hashlib.sha256(manifest.read_bytes()).hexdigest()!=gold['catalog_manifest_sha256']:raise ValueError('Gold catalog mismatch')
    products={p['id']:p for p in map(json.loads,(manifest.parent/'products.jsonl').read_text().splitlines())}
    settings=Settings.load(args.root); semaphore=asyncio.Semaphore(args.concurrency); results=[]
    async def run_case(case):
        async with semaphore:
            directory=args.output/case['id']; runtime=ShoppingRuntime(settings,runtime_dir=directory)
            cid=runtime.create_conversation(); outputs=[]; inputs=[]; harness_issue=None; started=time.monotonic()
            try:
                for step in case['steps']:
                    if isinstance(step,dict):
                        choices=[b for b in step['branches'] if outputs and outputs[-1]['kind']==b['if_kind'] and (not b.get('if_mentions') or any(w in outputs[-1]['message'] for w in b['if_mentions']))]
                        if not choices:
                            harness_issue='uncovered_branch'; break
                        text=choices[0]['reply']
                    else:text=step
                    inputs.append(text); outputs.append(await runtime.run(cid,text))
                events=[json.loads(line) for line in (directory/'trace.jsonl').read_text().splitlines()]
                checks=[]
                for i,o in enumerate(outputs):
                    turn_events=[e for e in events if e['turn_id']==o['turn_id']]
                    checks.append(check_turn(o,gold['cases'][case['id']][i],outputs,turn_events,runtime.conversation(cid),products))
                record={'id':case['id'],'split':case['split'],'family':case['family'],'inputs':inputs,'outputs':outputs,'failures':checks,'harness_issue':harness_issue,
                        'program_pass':not harness_issue and len(outputs)==len(case['steps']) and not any(checks),'semantic_review':'pending',
                        'elapsed_seconds':round(time.monotonic()-started,3)}
                (directory/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');results.append(record)
                print(json.dumps({'id':case['id'],'program_pass':record['program_pass'],'failures':checks,'harness_issue':harness_issue,'seconds':record['elapsed_seconds']},ensure_ascii=False),flush=True)
            finally:await runtime.close()
    try:await asyncio.gather(*(run_case(c) for c in selected))
    finally:
        report={'at':datetime.now(timezone.utc).isoformat(),'model':settings.model,'cases_sha256':cases_hash,
                'gold_sha256':gold_hash,'source_hashes':source_hashes,'selected':len(selected),'executed':len(results),
                'program_pass':sum(c['program_pass'] for c in results),'semantic_review':'pending','cases':sorted(results,key=lambda c:c['id'])}
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path.cwd());p.add_argument('--suite',type=Path,default=Path('eval'));p.add_argument('--output',type=Path,required=True)
    p.add_argument('--split',choices=['all','development','holdout'],default='development');p.add_argument('--ids');p.add_argument('--concurrency',type=int,choices=[1,2,3],default=2)
    asyncio.run(main(p.parse_args()))
