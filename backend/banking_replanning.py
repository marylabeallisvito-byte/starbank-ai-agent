"""Preview and explicitly accept source changes without repeating money effects."""
import json
from fastapi import Header, HTTPException
from pydantic import BaseModel
from backend import banking as b, banking_missions as m, banking_mission_evidence as e


class AcceptChanges(BaseModel):
    proposal_digest: str


def proposal(db,row):
    if row['status'] not in {'BLOCKED','PAUSED','WAITING_REVIEW'}: raise HTTPException(409,'仅暂停、受阻或待复核任务可以核对来源变化')
    inputs=json.loads(row['input_json']); nodes=json.loads(row['nodes_json'])
    saved=db.execute('SELECT * FROM banking_mission_sources WHERE mission_id=?',(row['id'],)).fetchone()
    if not saved: raise HTTPException(409,'缺少原始分析快照，请重新创建任务')
    original=json.loads(saved['payload'])
    if e.digest(original)!=saved['content_hash']: raise HTTPException(503,'原始快照完整性校验失败，不能调整任务')
    rows=e.read_rows(db,row['session_id'],inputs)
    current={r['id']:e.digest(dict(r)) for r in rows}
    previous=original.get('record_index')
    if previous is None: raise HTTPException(409,'此历史任务没有逐条来源索引，请重新创建任务')
    added=sorted(set(current)-set(previous)); removed=sorted(set(previous)-set(current))
    modified=sorted(k for k in current.keys() & previous.keys() if current[k]!=previous[k])
    if not (added or removed or modified): raise HTTPException(409,'当前任务范围内的账单来源没有变化')
    digest=e.digest({'mission':row['id'],'plan':row['digest'],'snapshot':saved['content_hash'],'current':current,'nodes':nodes,'status':row['status'],'released':row['released']})
    preserved=[n['id'] for n in nodes if n['status']=='COMPLETED' and n['id'] in {'reserve','cancel_subscription'}]
    return {'proposal_digest':digest,'added':added,'removed':removed,'modified':modified,'record_count':len(rows),'preserved_nodes':preserved,'requires_user_resume':True,
            'message':'仅重新核对账单与复核步骤；已完成的资金预留和订阅操作保留，不会重复执行。'},rows,inputs,nodes


@m.router.get('/missions/{mid}/changes')
def changes(mid: str,x_banking_session: str|None=Header(default=None)):
    with b._connect() as db:
        m.schema(db); user=b._session(db,x_banking_session); row=m.owned(db,user['id'],mid)
        result,*_=proposal(db,row)
        return result


@m.router.post('/missions/{mid}/replan')
def accept(mid: str,req: AcceptChanges,x_banking_session: str|None=Header(default=None)):
    with b._connect() as db:
        m.schema(db); db.execute('BEGIN IMMEDIATE'); user=b._session(db,x_banking_session); row=m.owned(db,user['id'],mid)
        result,rows,inputs,nodes=proposal(db,row)
        if req.proposal_digest!=result['proposal_digest']: raise HTTPException(409,'来源或任务再次变化，请重新核对差异')
        findings=m.duplicate_evidence(rows,inputs.get('bill_source','sandbox'))
        if db.execute("SELECT 1 FROM banking_reviews WHERE mission_id=? AND status IN ('PENDING','APPROVED','REJECTED')",(mid,)).fetchone(): inputs['replan_requires_review']=True
        e.capture(db,row,inputs,rows,findings)
        for node in nodes:
            if node['id']=='analyze': node.update(status='COMPLETED',output={'record_count':len(rows),'anomaly_count':len(findings),'note':'用户确认来源变化后重新核对'})
            elif node['id']=='risk_review' or (node['id']=='budget_check' and 'reserve' not in result['preserved_nodes']): node.update(status='PENDING',output=None)
        if inputs.get('bill_source')=='imported': inputs['source_manifest']=m.imported_snapshot(db,user['id'],inputs.get('bill_period'))[0]
        inputs['source_record_count']=len(rows)
        inputs['source_revision']=inputs.get('source_revision',0)+1
        inputs['accepted_source_hash']=e.input_hash(rows)
        new_digest=m.plan_digest(user['id'],mid,inputs,nodes)
        db.execute("UPDATE banking_reviews SET status='SUPERSEDED' WHERE mission_id=? AND status IN ('PENDING','APPROVED','REJECTED')",(mid,))
        db.execute("UPDATE banking_missions SET input_json=?,nodes_json=?,evidence_json=?,digest=?,status='PAUSED',reason=?,updated_at=? WHERE id=?",(json.dumps(inputs,ensure_ascii=False),json.dumps(nodes,ensure_ascii=False),json.dumps(findings,ensure_ascii=False),new_digest,'来源变化已核对，请检查更新后的任务并决定是否恢复',b._now().isoformat(),mid))
        m.event(db,row,'MISSION_REPLANNED',{'previous_digest':row['digest'],'new_digest':new_digest,'added_count':len(result['added']),'removed_count':len(result['removed']),'modified_count':len(result['modified']),'preserved_nodes':result['preserved_nodes']})
        return m.view(db,m.owned(db,user['id'],mid))
