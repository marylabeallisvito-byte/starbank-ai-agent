"""Read-only recovery and internal sandbox reconciliation. Never resubmits effects."""
import json
from fastapi import Header,HTTPException,Query
from backend import banking as b,banking_auth as auth,banking_wealth as wealth


@b.router.get('/execution/{kind}/{identifier}')
def recover(kind:str,identifier:str,expected_digest:str=Query(min_length=64,max_length=64),x_banking_session:str|None=Header(default=None)):
    table=auth.TABLES.get(kind)
    if not table:raise HTTPException(422,'不支持的操作类型')
    with b._connect() as db:
        db.execute('BEGIN')
        user=b._session(db,x_banking_session)
        row=db.execute(f'SELECT * FROM {table} WHERE id=? AND session_id=?',(identifier,user['id'])).fetchone()
        if not row:raise HTTPException(404,'当前会话没有此操作')
        if row['digest']!=expected_digest:raise HTTPException(409,'操作摘要与原记录不一致，不能恢复此授权')
        complete=row['status'] in {'COMMITTED','EXECUTED'}
        expired=not complete and row['expires_at']<=b._now().isoformat()
        locked=bool(user['locked_until'] and user['locked_until']>b._now().isoformat())
        known=row['status'] in {'PREPARED','AWAITING_VERIFICATION','READY'}
        next_step='view_receipt' if complete else 'prepare_again' if expired else 'wait_unlock' if locked else 'confirm' if row['status']=='PREPARED' else 'verify' if row['status']=='AWAITING_VERIFICATION' else 'review_then_continue' if known else 'contact_support'
        guidance={'view_receipt':'账本已确认完成，请核对原回执，无需重新提交。','prepare_again':'原预览已过期，请核对账本后重新预览。','wait_unlock':'账户暂时锁定，只能核查记录；解锁后重新查验原操作。','confirm':'原操作尚未确认，请核对原详情后明确确认。','verify':'原操作仍需设备验证，查验记录不会代替验证。','review_then_continue':'原操作尚未完成，请核对详情；继续时服务端仍会重查权限和业务条件。','contact_support':'原操作状态无法安全恢复，请保留记录并联系处理。'}[next_step]
        if kind=='transfer':result=b._view(row,db)
        elif kind=='wealth':result={**wealth._view(row),'demo_code':None}
        elif kind=='card':result={'action':{**b._card_action_view(row),'demo_code':None}}
        else:
            subscription=db.execute('SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=?',(row['subscription_id'],user['id'])).fetchone()
            result={'action':b._subscription_action_view(row),'subscription':b._subscription_view(subscription)}
        return {'kind':kind,'id':identifier,'status':row['status'],'complete':complete,'expired':expired,'can_continue':known and not expired and not locked,
                'next_step':next_step,'guidance':guidance,
                'checked_at':b._now().isoformat(),'read_only':True,'result':result,'scope':'仅核查当前虚构账户已保存的操作，不补发资金请求；继续仍需原业务校验。'}


def reconcile(db,user):
    sid=user['id']
    transfers=db.execute("SELECT COALESCE(SUM(amount_fen),0) FROM sandbox_transfers WHERE session_id=? AND status='COMMITTED'",(sid,)).fetchone()[0]
    orders=db.execute("SELECT product_id,action,amount_fen FROM sandbox_wealth_orders WHERE session_id=? AND status='COMMITTED'",(sid,)).fetchall()
    holdings={}
    net_buy=0
    for row in orders:
        delta=row['amount_fen']*(1 if row['action']=='BUY' else -1)
        net_buy+=delta;holdings[row['product_id']]=holdings.get(row['product_id'],0)+delta
    actual_holdings={row['product_id']:row['principal_fen'] for row in db.execute('SELECT product_id,principal_fen FROM sandbox_wealth_holdings WHERE session_id=?',(sid,))}
    reserved={'scheduled':0,'missions':0,'fx':0}
    reserved['scheduled']=db.execute("SELECT COALESCE(SUM(amount_fen),0) FROM sandbox_plans WHERE session_id=? AND status='COMPLETED' AND released_at IS NULL",(sid,)).fetchone()[0]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='banking_missions'").fetchone():
        for row in db.execute('SELECT input_json,nodes_json FROM banking_missions WHERE session_id=? AND released=0',(sid,)):
            if any(n['id']=='reserve' and n['status']=='COMPLETED' for n in json.loads(row['nodes_json'])):reserved['missions']+=b._amount_fen(json.loads(row['input_json'])['amount'])
    reserved['fx']=db.execute("SELECT COALESCE(SUM(reserved_fen),0) FROM fx_budget_quotes WHERE session_id=? AND status='RESERVED'",(sid,)).fetchone()[0]
    expected_cash=250000-transfers-net_buy
    expected_reserved=sum(reserved.values())
    checks=[{'id':'cash','title':'现金与已完成操作','matched':user['balance_fen']==expected_cash,'actual_fen':user['balance_fen'],'expected_fen':expected_cash},
            {'id':'reserved','title':'预留与有效预算','matched':user['reserved_fen']==expected_reserved,'actual_fen':user['reserved_fen'],'expected_fen':expected_reserved},
            {'id':'holdings','title':'持仓与申赎订单','matched':all(actual_holdings.get(k,0)==holdings.get(k,0) for k in actual_holdings.keys()|holdings.keys())}]
    return {'consistent':all(c['matched'] for c in checks),'checks':checks,'checked_at':b._now().isoformat(),'read_only':True,
            'reservation_sources_fen':reserved,'scope':'本地仿真账本内部一致性检查，初始现金为2500元；历史样例账单与外部数据不参与资金变动。非真实银行对账，不自动修账。'}


@b.router.get('/execution-reconciliation')
def reconciliation(x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        db.execute('BEGIN')
        return reconcile(db,b._session(db,x_banking_session))


@b.router.get('/execution-device-acceptance')
def device_acceptance(x_banking_session:str|None=Header(default=None)):
    with b._connect() as db:
        user=b._session(db,x_banking_session)
        row=db.execute("SELECT t.id,t.receipt,t.amount_fen,t.committed_at,a.detail FROM sandbox_transfers t JOIN sandbox_audit a ON a.transfer_id=t.id AND a.session_id=t.session_id AND a.event='PASSKEY_AUTHORIZED' WHERE t.session_id=? AND t.status='COMMITTED' AND t.level='red' ORDER BY t.committed_at DESC LIMIT 1",(user['id'],)).fetchone()
        return {'verified':bool(row),'result':{'id':row['id'],'receipt':row['receipt'],'amount':b._money(row['amount_fen']),'committed_at':row['committed_at'],'user_verified':json.loads(row['detail']).get('user_verified') is True} if row else None,
                'scope':'本会话红色仿真转账的通行密钥授权与账本完成记录；不证明银行实名身份或认证器硬件防伪。'}
