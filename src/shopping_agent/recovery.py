"""Bound retries by semantic blockers, not changing prose or assessment IDs."""
import json


class RejectionRecovery:
    def __init__(self):
        self.seen = {}

    def observe(self, name, payload, result, turn):
        errors = [e for e in result.get('errors', []) if e.get('code') == 'requirement_intent_conflict']
        hard_error = name == 'finish_turn' and str(result.get('error','')).startswith('unverified hard constraints:')
        if not errors and not hard_error:
            return None
        state = turn.state() or {}
        scope = payload.get('scope', 'formal')
        if errors:
            # Renaming the group or predicate is not semantic progress.
            reason = ['requirement_intent_conflict', sorted((e.get('scope','formal'),e['quote']) for e in errors)]
            evidence = None
        else:
            detail = json.loads(result['error'].split(':',1)[1])
            reason = ['unverified_hard_constraints', {k: sorted(detail[k]) for k in ['violated','unknown','conflict']}]
            blocked = set(detail['violated'] + detail['unknown'] + detail['conflict'])
            evidence = {key: value for key,value in detail.get('checks',{}).items() if key in blocked}
        marker = json.dumps([reason, scope, state.get('requirements'),state.get('exploration'),
                             state.get('excluded'), evidence], sort_keys=True, ensure_ascii=False, default=str)
        self.seen[marker] = self.seen.get(marker,0) + 1
        repair = {'code':reason[0], 'attempts':self.seen[marker],
                  'next_action':'Correct the quoted interpretation before writing.' if errors else
                  'Keep genuine requirements; supply new relevant evidence or finish data_limited. Rewording the same recommendation does not repair eligibility.'}
        result['recovery'] = repair
        if self.seen[marker] < 2:
            return None
        message = ('这次还没能准确理解“'+ '；'.join(e['quote'] for e in errors) +'”。有争议的修改尚未生效，已生效的其他要求会保留。请补充说明这是不需要某项功能，还是明确不要这类商品。' if errors else
                   '现有资料还无法支持符合当前要求的推荐，本轮未完成。你的要求已保留；可以补充说明哪些要求可以调整，也可以先维持原要求。')
        return {'kind':'execution_limited','message':message,'product_ids':[],'citations':[],
                'unresolved':['repeated_'+reason[0]],'scope':scope,'turn_id':turn.turn_id,
                'task_id':turn.task_id,'display_id':None,'state':turn.state()}
