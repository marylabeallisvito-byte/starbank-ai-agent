"""Task provenance: actual rule inputs, not unrelated recipient demo graphs."""
import hashlib
import json
from fastapi import Header, HTTPException, Query
from backend import banking as b, banking_missions as m


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def record_view(db,sid,row,source):
    result={"id":row["id"],"merchant":row["merchant"],"amount":b._money(row["amount_fen"]),"occurred_at":row["occurred_at"],"currency":"CNY","source":source}
    if source=='imported':
        item=db.execute("SELECT r.batch_id,d.filename,d.file_hash FROM statement_records r JOIN statement_drafts d ON r.batch_id=d.id AND r.session_id=d.session_id WHERE r.session_id=? AND r.fingerprint=?",(sid,row['id'].removeprefix('IMPORT-'))).fetchone()
        if not item: raise ValueError('来源批次不存在')
        result.update(batch_id=item['batch_id'],filename=item['filename'],file_hash=item['file_hash'])
    return result


def read_rows(db,sid,inputs):
    if inputs.get('bill_source')=='imported': return m.imported_snapshot(db,sid,inputs.get('bill_period'))[1]
    from datetime import datetime
    period=inputs.get('bill_period') or b._now().astimezone(b.TZ).strftime('%Y-%m')
    return [dict(r) for r in db.execute('SELECT id,merchant,amount_fen,occurred_at FROM sandbox_bills WHERE session_id=? ORDER BY id',(sid,)) if datetime.fromisoformat(r['occurred_at']).astimezone(b.TZ).strftime('%Y-%m')==period]


def input_hash(rows): return digest(sorted([dict(r) for r in rows],key=lambda r:r['id']))


def verify_before_effect(db,row,inputs,rows):
    saved=db.execute('SELECT * FROM banking_mission_sources WHERE mission_id=? AND session_id=?',(row['id'],row['session_id'])).fetchone()
    if not saved:
        if inputs.get('evidence_version'): raise ValueError('缺少任务来源快照，已停止资金与订阅操作')
        return
    snapshot=json.loads(saved['payload'])
    if digest(snapshot)!=saved['content_hash'] or snapshot.get('input_hash')!=input_hash(rows):
        raise ValueError('任务账单来源已变化或校验失败，已停止资金与订阅操作')


def capture(db,row,inputs,rows,findings):
    cited={identifier for f in findings for identifier in f['transaction_ids']}
    records=[record_view(db,row['session_id'],r,inputs.get('bill_source','sandbox')) for r in rows if r['id'] in cited]
    snapshot={"version":1,"source":inputs.get('bill_source','sandbox'),"period":inputs.get('bill_period'),"record_count":len(rows),"input_hash":input_hash(rows),"record_index":{r["id"]:digest(dict(r)) for r in rows},"records":records,"findings":findings,"captured_at":b._now().isoformat()}
    db.execute('INSERT OR REPLACE INTO banking_mission_sources VALUES(?,?,?,?,?)',(row['id'],row['session_id'],json.dumps(snapshot,ensure_ascii=False),digest(snapshot),snapshot['captured_at']))


def redact_batch(db,sid,batch_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='banking_mission_sources'").fetchone(): return
    for saved in db.execute('SELECT * FROM banking_mission_sources WHERE session_id=?',(sid,)).fetchall():
        snapshot=json.loads(saved['payload'])
        affected={r['id'] for r in snapshot['records'] if r.get('batch_id')==batch_id}
        if not affected: continue
        snapshot['records']=[{"id":r['id'],"batch_id":batch_id,"redacted":True} if r['id'] in affected else r for r in snapshot['records']]
        for f in snapshot['findings']:
            if affected.intersection(f['transaction_ids']):
                f.update(merchant='来源已清除',amount='0.00',explanation='该线索的部分来源已由用户清除，不能继续据此核查。')
        db.execute('UPDATE banking_mission_sources SET payload=?,content_hash=? WHERE mission_id=? AND session_id=?',(json.dumps(snapshot,ensure_ascii=False),digest(snapshot),saved['mission_id'],sid))
        db.execute('UPDATE banking_missions SET evidence_json=?,updated_at=? WHERE id=? AND session_id=?',(json.dumps(snapshot['findings'],ensure_ascii=False),b._now().isoformat(),saved['mission_id'],sid))


@m.router.get('/missions/{mid}/evidence')
def mission_evidence(mid: str, finding: int=Query(default=0,ge=0), x_banking_session: str|None=Header(default=None)):
    with b._connect() as db: sid=b._session(db,x_banking_session)['id']
    return evidence_for_owner(mid,finding,sid)


def evidence_for_owner(mid,finding,sid):
    with b._connect() as db:
        m.schema(db); row=m.owned(db,sid,mid)
        saved=db.execute('SELECT * FROM banking_mission_sources WHERE mission_id=? AND session_id=?',(mid,sid)).fetchone()
        inputs=json.loads(row['input_json'])
        if not saved:
            return {"status":"pending" if not any(n['id']=='analyze' and n['status']=='COMPLETED' for n in json.loads(row['nodes_json'])) else "legacy_unavailable","mission_id":mid,"message":"任务尚未完成来源采集" if row['status']=='DRAFT' else "此任务尚无可验证来源快照；历史任务不会补造证据。"}
        try: snapshot=json.loads(saved['payload'])
        except ValueError: raise HTTPException(503,'任务证据快照无法解析') from None
        if digest(snapshot)!=saved['content_hash']: raise HTTPException(503,'任务证据完整性校验失败')
        findings=snapshot['findings']
        if findings and finding>=len(findings): raise HTTPException(404,'证据条目不存在')
        selected=findings[finding] if findings else None
        ids=set(selected['transaction_ids']) if selected else set()
        records=[r for r in snapshot['records'] if r['id'] in ids]
        for record in records:
            if record.get('redacted'):
                record['current_status']='cleared'; continue
            if snapshot['source']=='imported':
                live=db.execute('SELECT payload FROM statement_records WHERE session_id=? AND fingerprint=?',(sid,record['id'].removeprefix('IMPORT-'))).fetchone()
                if not live: record['current_status']='missing'; continue
                data=json.loads(live['payload'])
                same=data['merchant']==record['merchant'] and data['amount']==record['amount'] and data['date']==record['occurred_at'] and data['currency']=='CNY'
            else:
                live=db.execute('SELECT * FROM sandbox_bills WHERE session_id=? AND id=?',(sid,record['id'])).fetchone()
                same=bool(live) and live['merchant']==record['merchant'] and b._money(live['amount_fen'])==record['amount'] and live['occurred_at']==record['occurred_at']
            record['current_status']='matching' if same else 'changed' if live else 'missing'
        manifest_changed=snapshot.get('input_hash')!=input_hash(read_rows(db,sid,inputs)) or (snapshot['source']=='imported' and m.imported_snapshot(db,sid,inputs.get('bill_period'))[0]!=inputs.get('source_manifest'))
        reviews=[dict(r) for r in db.execute('SELECT status,decision_note FROM banking_reviews WHERE mission_id=? ORDER BY created_at',(mid,))]
    now=snapshot['captured_at']
    nodes=[{"id":"mission","labels":["Task"],"properties":{"name":"当前任务"}}]
    edges=[]
    if selected:
        nodes.append({"id":"finding","labels":["Finding"],"properties":{"name":"疑似重复记录"}})
        edges.append({"id":"effect","source":"finding","target":"mission","type":"AFFECTS","properties":{"date":now}})
        for index,record in enumerate(records):
            rid=f'record-{index}'
            nodes.append({"id":rid,"labels":["Record"],"properties":{"name":"已清除记录" if record.get('redacted') else f"记录 {index+1} · ¥{record['amount']}"}})
            edges.append({"id":record['id'],"source":rid,"target":"finding","type":"SUPPORTS","properties":{"date":record.get('occurred_at',now)}})
            batch=record.get('batch_id','sandbox')
            bid='batch-'+batch
            if not any(n['id']==bid for n in nodes): nodes.append({"id":bid,"labels":["Document"],"properties":{"name":"已清除来源" if record.get('redacted') else record.get('filename','本月仿真账本')}})
            edges.append({"id":"origin-"+rid,"source":bid,"target":rid,"type":"INCLUDES","properties":{"date":now}})
    source_changed=manifest_changed or any(r['current_status']!='matching' for r in records)
    return {"status":"changed" if source_changed else "available","mission_id":mid,"mission_status":row['status'],"captured_at":now,"snapshot_hash":saved['content_hash'],"record_count":snapshot['record_count'],"source":snapshot['source'],"period":snapshot['period'],"finding_count":len(findings),"finding_index":finding,"finding":selected,"records":records,"reviews":reviews,
            "authorization_effect":"任务来源已变化，不能据此继续授权；请先核对变更。" if source_changed else "发现疑似重复时进入独立人工复核；批准后仍须用户恢复，来源记录本身不能批准资金操作。" if selected else "当前重复记录规则未命中，其他风险不在本次核查范围；预算操作仍受已确认计划与余额条件约束。",
            "network":{"recipient_id":"mission","revision":saved['content_hash']+str(finding),"coverage":"mission_snapshot","observed_at":now,"expires_at":None,"source_status":"fresh","decision":"standard","summary":"任务来源关系，不是收款人风险图谱。","generation":"task_provenance","policy_version":"mission-evidence-v1","graph":{"nodes":nodes,"edges":edges},"findings":[]}}


@m.router.get('/operator/reviews/{rid}/evidence')
def operator_evidence(rid: str, finding: int=Query(default=0,ge=0), x_banking_operator: str|None=Header(default=None)):
    actor=m.operator(x_banking_operator)
    with b._connect() as db:
        m.schema(db)
        review=db.execute("SELECT * FROM banking_reviews WHERE id=? AND status='PENDING'",(rid,)).fetchone()
        if not review: raise HTTPException(404,'待复核任务不存在或已处理')
        row=db.execute("SELECT * FROM banking_missions WHERE id=? AND status='WAITING_REVIEW'",(review['mission_id'],)).fetchone()
        if not row: raise HTTPException(409,'任务状态已变化，请刷新复核队列')
        mid,sid=row['id'],row['session_id']
    result=evidence_for_owner(mid,finding,sid)
    with b._connect() as db:
        m.event(db,row,'OPERATOR_EVIDENCE_READ',{'review_id':rid,'snapshot_hash':result.get('snapshot_hash'),'actor':actor['id'],'role':actor['role']})
    return result
