"""User-facing arrangement projection; amounts come from the owned local ledger.

Reservation is not expenditure. A spending floor is not a frozen rent payment.
Recurring charge amounts are context, never claimed as realized savings/refunds.
"""
import json
from backend import banking as b


def amount_fen(value):
    return 0 if value in (None,'0','0.00') else b._amount_fen(value)


def summary(db,user,inputs,nodes=None,status='DRAFT',released=False,findings=None,preflight=False):
    nodes=nodes or [];findings=findings or []
    reserve=next((n for n in nodes if n['id']=='reserve'),None)
    cancel=next((n for n in nodes if n['id']=='cancel_subscription'),None)
    planned=amount_fen(inputs.get('amount'));floor=amount_fen(inputs.get('minimum_balance'))
    complete=inputs.get('amount') is not None and inputs.get('minimum_balance') is not None
    applied=bool(reserve and reserve['status']=='COMPLETED' and not released)
    ended=released or status in {'CANCELLED','PARTIAL_STOPPED'}
    effective=0 if ended else planned
    other_reserved=user['reserved_fen']-(planned if applied else 0)
    headroom=user['balance_fen']-other_reserved-effective-floor
    policy=inputs.get('risk_policy','pause_all_on_suspicion')
    subscription=None
    if inputs.get('cancel_subscription'):
        row=db.execute('SELECT merchant,amount_fen,next_charge_at,status FROM sandbox_subscriptions WHERE id=? AND session_id=?',(inputs.get('subscription_id'),user['id'])).fetchone()
        if row:
            subscription={'merchant':row['merchant'],'scheduled_amount_fen':row['amount_fen'],'next_charge_at':row['next_charge_at'],'current_status':row['status'],'source':'local_synthetic_subscription','savings_verified':False,'refund_included':False}
    reserve_state='released' if released else 'stopped' if ended else 'held' if applied else 'planned'
    cancel_state='not_requested' if not inputs.get('cancel_subscription') else 'cancelled' if cancel and cancel['status']=='COMPLETED' else 'stopped' if ended else 'waiting_review' if status in {'WAITING_REVIEW','BLOCKED'} else 'pending'
    review=next((n for n in nodes if n['id']=='risk_review'),None)
    analyzed=preflight or any(n['id']=='analyze' and n['status']=='COMPLETED' for n in nodes)
    effects=[{'id':'reserve','title':'预算预留','state':reserve_state,'amount_fen':effective,'remaining':reserve_state=='planned'},
             {'id':'cancel_subscription','title':'指定订阅取消','state':cancel_state,'remaining':cancel_state in {'pending','waiting_review'}}]
    return {'period':inputs.get('bill_period'),'as_of':b._now().isoformat(),'source':inputs.get('bill_source','sandbox'),
            'funding':{'complete':complete,'budget_known':inputs.get('amount') is not None,'floor_known':inputs.get('minimum_balance') is not None,'cash_total_fen':user['balance_fen'],'cash_available_fen':b._available(user),'existing_other_reserved_fen':other_reserved,'target_budget_fen':planned,'plan_budget_fen':effective,'minimum_balance_fen':floor,'headroom_fen':headroom if complete else None,'shortfall_fen':max(0,-headroom) if complete else None,'enough':complete and headroom>=0 and other_reserved>=0,'budget_state':reserve_state,'projection':not applied and not ended},
            'effects':effects,'subscription':subscription,'exception':{'analysis_checked':analyzed,'finding_count':len(findings) if analyzed else None,'review_complete':bool(review and review['status']=='COMPLETED'),'policy':policy,'budget_may_be_reserved_before_review':policy=='reserve_before_review','cancellation_waits_for_review':True},
            'authorization':{'money_leaves_account':False,'includes_transfer':False,'includes_wealth':False,'includes_refund':False,'subscription_cancel_requested':bool(inputs.get('cancel_subscription'))},
            'scope':'当前本地虚构账户的现金与预留；不把导入账单、疑似重复扣费或退款预期计作可用资金。生活费底线为执行条件，不表示已支付或冻结房租。'}


def goal_summary(db,user,inputs):
    from backend import banking_mission_evidence as evidence,banking_missions as missions
    rows=evidence.read_rows(db,user['id'],inputs)
    return summary(db,user,inputs,findings=missions.duplicate_evidence(rows,inputs.get('bill_source','sandbox')),preflight=True)


def mission_summary(db,row,inputs,nodes):
    user=db.execute('SELECT * FROM sandbox_sessions WHERE id=?',(row['session_id'],)).fetchone()
    return summary(db,user,inputs,nodes,row['status'],bool(row['released']),json.loads(row['evidence_json']))
