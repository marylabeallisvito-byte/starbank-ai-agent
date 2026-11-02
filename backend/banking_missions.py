"""Durable, bounded multi-business tasks. Each node and its effect commit together."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from backend.banking import _connect, _session, _audit, _now, _money, _amount_fen, _available, _sandbox_enabled, interpret_intent, IntentRequest, TZ

router=APIRouter(prefix="/api/banking",tags=["banking-missions"],dependencies=[Depends(_sandbox_enabled)])

def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS banking_missions(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,input_json TEXT NOT NULL,nodes_json TEXT NOT NULL,evidence_json TEXT NOT NULL DEFAULT '[]',digest TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,released INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS banking_mission_events(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,event TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS banking_reviews(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,node_id TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,decision_note TEXT,created_at TEXT NOT NULL,decided_at TEXT);
    CREATE TABLE IF NOT EXISTS banking_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,client_id TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,attachment TEXT,created_at TEXT NOT NULL,UNIQUE(session_id,client_id,role));
    CREATE TABLE IF NOT EXISTS banking_mission_sources(mission_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
    """)
    columns={r['name'] for r in db.execute('PRAGMA table_info(banking_missions)')}
    if 'not_before' not in columns:
        db.execute('ALTER TABLE banking_missions ADD COLUMN not_before TEXT')
        for row in db.execute('SELECT id,input_json FROM banking_missions').fetchall():
            start=json.loads(row['input_json']).get('not_before')
            if start:db.execute('UPDATE banking_missions SET not_before=? WHERE id=?',(datetime.fromisoformat(start).astimezone(timezone.utc).isoformat(),row['id']))
    db.execute('CREATE INDEX IF NOT EXISTS banking_mission_due ON banking_missions(status,not_before,updated_at)')
    from backend.banking_storage import install
    install(db)

class MissionRequest(BaseModel):
    amount: str
    minimum_balance: str="0.00"
    subscription_id: str|None=None
    cancel_subscription: bool=False
    bill_source: Literal["sandbox", "imported"]="sandbox"

class MissionAction(BaseModel):
    action: Literal["confirm","pause","resume","cancel","release","handoff"]
    digest: str|None=None
    reason: str=Field(default="用户请求人工接管",max_length=500)

class MissionRevision(BaseModel):
    amount: str
    minimum_balance: str
    digest: str


def plan_digest(sid, mid, inputs, nodes):
    structure = [{k: n[k] for k in ("id", "title", "level", "depends_on")} for n in nodes]
    return hashlib.sha256(json.dumps([sid, mid, inputs, structure], sort_keys=True).encode()).hexdigest()


def validate_dag(nodes):
    allowed = {"analyze", "budget_check", "risk_review", "reserve", "cancel_subscription", "notify"}
    ids = {n["id"] for n in nodes}
    if len(ids) != len(nodes) or not ids <= allowed or not {"analyze", "risk_review", "reserve", "notify"} <= ids:
        raise ValueError("任务结构不完整或包含未知工具")
    completed = set()
    while len(completed) < len(nodes):
        ready = {n["id"] for n in nodes if n["id"] not in completed and set(n["depends_on"]) <= completed}
        if not ready: raise ValueError("任务依赖存在循环或缺失")
        completed |= ready


def imported_snapshot(db, sid, period=None):
    records = db.execute("SELECT fingerprint,payload FROM statement_records WHERE session_id=? ORDER BY fingerprint", (sid,)).fetchall()
    rows = []
    from datetime import datetime
    for item in records:
        record = json.loads(item["payload"])
        if record["currency"] != "CNY" or Decimal(record["amount"]) <= 0: continue
        date = datetime.fromisoformat(record["date"])
        if date.strftime('%Y-%m') != (period or _now().astimezone(TZ).strftime('%Y-%m')): continue
        rows.append({"id": "IMPORT-"+item["fingerprint"], "merchant":record["merchant"], "amount_fen":int(Decimal(record["amount"])*100), "occurred_at":record["date"]})
    manifest=hashlib.sha256(json.dumps(sorted(rows,key=lambda r:r["id"]),sort_keys=True).encode()).hexdigest()
    return manifest, rows


def duplicate_evidence(rows, source):
    from datetime import datetime
    grouped={}
    for row in rows:
        when=datetime.fromisoformat(row["occurred_at"])
        if when.tzinfo is None: when=when.replace(tzinfo=TZ)
        grouped.setdefault((row["merchant"],row["amount_fen"]),[]).append((when,row))
    found=[]
    for items in grouped.values():
        items.sort(key=lambda item:item[0])
        for (lt,left),(rt,right) in zip(items,items[1:]):
            if (rt-lt).total_seconds()<=120:
                found.append({"kind":"possible_duplicate","merchant":left["merchant"],"amount":_money(left["amount_fen"]),"transaction_ids":[left["id"],right["id"]],"explanation":"记录时间两分钟内同商户同金额；日期精度可能仅到日，需人工核对，不等于确认误扣","source":source})
    return found

class ReviewDecision(BaseModel):
    evidence_digest: str|None=None
    decision: Literal["approve","reject"]
    note: str=Field(min_length=3,max_length=500)

class MessageRequest(BaseModel):
    text: str=Field(min_length=1,max_length=500)
    client_id: str=Field(min_length=8,max_length=100)
    goal_id: str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    goal_digest: str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')

def event(db,row,name,detail):
    now=_now().isoformat()
    db.execute("INSERT INTO banking_mission_events(mission_id,event,detail,created_at) VALUES(?,?,?,?)",(row["id"],name,json.dumps(detail,ensure_ascii=False),now))
    _audit(db,row["session_id"],row["id"],name,detail)

def owned(db,sid,mid):
    row=db.execute("SELECT * FROM banking_missions WHERE id=? AND session_id=?",(mid,sid)).fetchone()
    if not row: raise HTTPException(404,"任务不存在")
    return row

def view(db,row):
    from backend.banking_monthly import mission_summary
    inputs=json.loads(row['input_json']);nodes=json.loads(row['nodes_json'])
    monthly=mission_summary(db,row,inputs,nodes)
    return {'monthly':monthly,"id":row["id"],"title":row["title"],"status":row["status"],"input":json.loads(row["input_json"]),"nodes":json.loads(row["nodes_json"]),"evidence":json.loads(row["evidence_json"]),"digest":row["digest"],"reason":row["reason"],"released":bool(row["released"]),"created_at":row["created_at"],"updated_at":row["updated_at"],"events":[dict(x) for x in db.execute("SELECT event,detail,created_at FROM banking_mission_events WHERE mission_id=? ORDER BY id",(row["id"],))],"reviews":[dict(x) for x in db.execute("SELECT id,node_id,status,reason,decision_note,created_at,decided_at FROM banking_reviews WHERE mission_id=? ORDER BY created_at",(row["id"],))],"sandbox":True}

@router.post("/missions/prepare")
def prepare(req: MissionRequest,x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session)
        return prepare_in_db(db,user,req)


def prepare_in_db(db,user,req,constraints=None):
    """Compile in the caller's transaction; drafts and proposal links commit together."""
    amount=_amount_fen(req.amount)
    floor=0 if req.minimum_balance in {"0","0.00"} else _amount_fen(req.minimum_balance)
    subscription=None
    if req.cancel_subscription:
        subscription=db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=? AND status='ACTIVE'",(req.subscription_id,user["id"])).fetchone()
        if not subscription: raise HTTPException(422,"请指定当前生效的模拟订阅")
    inputs={"amount":_money(amount),"minimum_balance":_money(floor),"subscription_id":req.subscription_id,"subscription_name":subscription["merchant"] if subscription else None,"cancel_subscription":req.cancel_subscription,"plan_version":"dag-v2"}
    if constraints: inputs.update(constraints)
    inputs["bill_source"]=req.bill_source
    inputs["evidence_version"]="task-source-v1"
    inputs["bill_period"]=inputs.get('bill_period') or _now().astimezone(TZ).strftime('%Y-%m')
    if req.bill_source=="imported":
        manifest, imported=imported_snapshot(db,user["id"],inputs['bill_period'])
        if not imported: raise HTTPException(422,"当前没有已确认导入的本月人民币支出，请先导入并核对账单")
        inputs.update(source_manifest=manifest,source_record_count=len(imported))
    specs=[("analyze","核对账单证据","green"),("budget_check","检查预算与生活费条件","green"),("risk_review","检查异常与人工复核","green"),("reserve","预留预算并保留生活费","yellow")]
    if req.cancel_subscription: specs.append(("cancel_subscription","取消指定模拟订阅","yellow"))
    specs.append(("notify","生成完成通知","green"))
    dependencies={"analyze":[],"budget_check":[],"risk_review":["analyze"],"reserve":["risk_review","budget_check"],"cancel_subscription":["reserve"],"notify":["reserve"]+(["cancel_subscription"] if req.cancel_subscription else [])}
    nodes=[{"id":id_,"title":title,"level":level,"depends_on":dependencies[id_],"status":"PENDING","output":None} for id_,title,level in specs]
    if inputs.get('risk_policy')=='reserve_before_review':
        if inputs.get('risk_policy_acknowledged') is not True:raise HTTPException(422,'预算先预留需要用户明确选择并确认异常处置范围')
        for node in nodes:
            if node['id']=='reserve':node['depends_on']=['analyze','budget_check']
            elif node['id']=='risk_review':node['depends_on']=['analyze','reserve']
            elif node['id']=='cancel_subscription':node['depends_on']=['reserve','risk_review']
            elif node['id']=='notify':node['depends_on']=list(dict.fromkeys(node['depends_on']+['risk_review']))
    validate_dag(nodes)
    mid=secrets.token_hex(16); now=_now().isoformat()
    digest=plan_digest(user["id"],mid,inputs,nodes)
    title="账单体检 · 预留预算"+(" · 订阅整理" if subscription else "")
    db.execute("INSERT INTO banking_missions(id,session_id,title,status,input_json,nodes_json,digest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(mid,user["id"],title,"DRAFT",json.dumps(inputs,ensure_ascii=False),json.dumps(nodes,ensure_ascii=False),digest,now,now))
    if inputs.get('not_before'):
        db.execute('UPDATE banking_missions SET not_before=? WHERE id=?',(datetime.fromisoformat(inputs['not_before']).astimezone(timezone.utc).isoformat(),mid))
    row=owned(db,user["id"],mid); event(db,row,"MISSION_PREPARED",{"node_ids":[n["id"] for n in nodes],"digest":digest})
    return view(db,row)


@router.post("/missions/{mid}/revise")
def revise(mid: str, req: MissionRevision, x_banking_session: str|None=Header(default=None)):
    amount=_amount_fen(req.amount)
    floor=0 if req.minimum_balance in {"0", "0.00"} else _amount_fen(req.minimum_balance)
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session); row=owned(db,user["id"],mid)
        if row["status"] != "DRAFT" or row["created_at"] < (_now()-timedelta(minutes=30)).isoformat(): raise HTTPException(409,"仅可修改有效期内尚未启用的草案")
        if not secrets.compare_digest(req.digest,row["digest"]): raise HTTPException(409,"计划已变化，请先重新读取")
        nodes=json.loads(row["nodes_json"]); inputs=json.loads(row["input_json"])
        if inputs.get("plan_version") != "dag-v2": raise HTTPException(409,"此历史草案请重新创建后修改")
        validate_dag(nodes)
        previous={"amount":inputs["amount"],"minimum_balance":inputs["minimum_balance"]}
        inputs.update(amount=_money(amount),minimum_balance=_money(floor))
        if inputs.get('constraint_provenance'):
            for key in ('amount','minimum_balance'):inputs['constraint_provenance'][key]={'source':'user_edit','quote':'由你在执行计划草案中修改'}
        digest=plan_digest(user["id"],mid,inputs,nodes)
        db.execute("UPDATE banking_missions SET input_json=?,digest=?,updated_at=? WHERE id=?",(json.dumps(inputs,ensure_ascii=False),digest,_now().isoformat(),mid))
        event(db,row,"MISSION_REVISED",{"before":previous,"after":{"amount":inputs["amount"],"minimum_balance":inputs["minimum_balance"]},"digest":digest})
        return view(db,owned(db,user["id"],mid))

@router.get("/missions")
def list_missions(x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db); user=_session(db,x_banking_session)
        return {"items":[view(db,row) for row in db.execute("SELECT * FROM banking_missions WHERE session_id=? ORDER BY created_at DESC LIMIT 40",(user["id"],))]}

def handoff(db,row,node_id,reason):
    if not db.execute("SELECT 1 FROM banking_reviews WHERE mission_id=? AND status='PENDING'",(row["id"],)).fetchone():
        db.execute("INSERT INTO banking_reviews(id,mission_id,node_id,status,reason,created_at) VALUES(?,?,?,'PENDING',?,?)",(secrets.token_hex(16),row["id"],node_id,reason,_now().isoformat()))
    db.execute("UPDATE banking_missions SET status='WAITING_REVIEW',reason=?,updated_at=? WHERE id=?",(reason,_now().isoformat(),row["id"]))
    event(db,row,"HUMAN_REVIEW_REQUESTED",{"node_id":node_id,"reason":reason})

def validate_confirmation(db,user,row,digest):
    nodes=json.loads(row['nodes_json']);inputs=json.loads(row['input_json'])
    if row["status"]!="DRAFT" or row["created_at"]<(_now()-timedelta(minutes=30)).isoformat(): raise HTTPException(409,"任务草案已失效")
    if not digest or not secrets.compare_digest(digest,row["digest"]): raise HTTPException(409,"确认摘要不一致")
    try: validate_dag(nodes)
    except ValueError as error: raise HTTPException(409,str(error)) from None
    if inputs.get("plan_version")=="dag-v2" and not secrets.compare_digest(row["digest"],plan_digest(user["id"],row['id'],inputs,nodes)): raise HTTPException(409,"任务结构校验失败，请重新生成计划")
    if inputs.get("bill_source")=="imported" and imported_snapshot(db,user["id"],inputs.get("bill_period"))[0]!=inputs["source_manifest"]: raise HTTPException(409,"导入来源已变化，请重新生成计划后确认")
    if user["locked_until"] and user["locked_until"]>_now().isoformat(): raise HTTPException(423,"账户暂时锁定")


@router.post("/missions/{mid}/action")
def action(mid: str,req: MissionAction,x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session); row=owned(db,user["id"],mid)
        state=row["status"]; nodes=json.loads(row["nodes_json"]); inputs=json.loads(row["input_json"])
        if req.action=="confirm":
            validate_confirmation(db,user,row,req.digest)
            next_state="RUNNING"
        elif req.action=="pause":
            if state!="RUNNING": raise HTTPException(409,"当前不能暂停")
            next_state="PAUSED"
        elif req.action=="resume":
            if state not in {"PAUSED","BLOCKED"}: raise HTTPException(409,"请等待人工决定，或任务当前不可恢复")
            if db.execute("SELECT 1 FROM banking_reviews WHERE mission_id=? AND status IN ('PENDING','REJECTED')",(mid,)).fetchone(): raise HTTPException(403,"人工复核尚未批准")
            next_state="RUNNING"
        elif req.action=="handoff":
            if state not in {"RUNNING","PAUSED","BLOCKED"}: raise HTTPException(409,"当前不能接管")
            handoff(db,row,None,req.reason)
            return view(db,owned(db,user["id"],mid))
        elif req.action in {"cancel","release"}:
            if req.action=="release" and state!="COMPLETED": raise HTTPException(409,"仅已完成任务可以释放预算")
            if req.action=="cancel" and state in {"COMPLETED","CANCELLED","PARTIAL_STOPPED"}: raise HTTPException(409,"任务已结束")
            reserved=next(n for n in nodes if n["id"]=="reserve")
            if reserved["status"]=="COMPLETED" and not row["released"]:
                fen=_amount_fen(inputs["amount"])
                db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen-? WHERE id=? AND reserved_fen>=?",(fen,user["id"],fen))
                if db.execute("SELECT changes()").fetchone()[0]!=1: raise HTTPException(409,"预算状态不一致")
                db.execute("UPDATE banking_missions SET released=1 WHERE id=?",(mid,))
                event(db,row,"MISSION_BUDGET_COMPENSATED",{"amount":inputs["amount"]})
            irreversible=any(n["id"]=="cancel_subscription" and n["status"]=="COMPLETED" for n in nodes)
            next_state=state if req.action=="release" else "PARTIAL_STOPPED" if irreversible else "CANCELLED"
            db.execute("UPDATE banking_reviews SET status='CANCELLED' WHERE mission_id=? AND status='PENDING'",(mid,))
        db.execute("UPDATE banking_missions SET status=?,reason=NULL,updated_at=? WHERE id=?",(next_state,_now().isoformat(),mid))
        event(db,row,"MISSION_"+req.action.upper(),{"from":state,"to":next_state})
        return view(db,owned(db,user["id"],mid))

def tick(mid: str):
    # Read-only report outside the write lock. Every mutating node rechecks its own preconditions.
    with _connect() as db:
        schema(db); row=db.execute("SELECT * FROM banking_missions WHERE id=?",(mid,)).fetchone()
        if not row or row["status"]!="RUNNING": return False
        sid=row["session_id"]
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT * FROM banking_missions WHERE id=?",(mid,)).fetchone()
        if row["status"]!="RUNNING": return False
        user=db.execute("SELECT * FROM sandbox_sessions WHERE id=?",(sid,)).fetchone()
        nodes=json.loads(row["nodes_json"]); inputs=json.loads(row["input_json"])
        from backend.banking_mission_evidence import read_rows, verify_before_effect
        bill_rows=read_rows(db,sid,inputs)
        node=None
        try:
            validate_dag(nodes)
            if inputs.get("plan_version")=="dag-v2" and not secrets.compare_digest(row["digest"],plan_digest(sid,mid,inputs,nodes)): raise ValueError("授权后的计划结构发生变化，已停止执行")
            if inputs.get('not_before'):
                from datetime import datetime
                start=datetime.fromisoformat(inputs['not_before'])
                if start.tzinfo is None: raise ValueError('执行时间缺少时区，已停止任务')
                if _now()<start: return False
            if inputs.get("bill_source")=="imported":
                manifest,bill_rows=imported_snapshot(db,sid,inputs.get("bill_period"))
                if manifest!=inputs["source_manifest"]: raise ValueError("导入账单来源已变化，已停止后续步骤；请取消后重新规划")
            completed={n["id"] for n in nodes if n["status"]=="COMPLETED"}
            node=next((n for n in nodes if n["status"]!="COMPLETED" and set(n["depends_on"])<=completed),None)
            if node is None: raise ValueError("任务没有可执行的就绪节点")
            if user["expires_at"]<=_now().isoformat() or (user["locked_until"] and user["locked_until"]>_now().isoformat()): raise ValueError("会话过期或账户安全锁定")
            if node['id']=='reserve' and inputs.get('risk_policy')=='reserve_before_review':
                if inputs.get('risk_policy_acknowledged') is not True:raise ValueError('缺少异常处置范围的明确确认')
                findings=json.loads(row['evidence_json'])
                if any(f.get('kind')!='possible_duplicate' for f in findings):raise ValueError('存在超出预算先预留范围的风险，已停止办理')
                if inputs.get('replan_requires_review') and not any(n['id']=='risk_review' and n['status']=='COMPLETED' for n in nodes):
                    handoff(db,row,'risk_review','来源变更后，预算预留前需要重新复核')
                    return True
            if node["id"] in {"reserve","cancel_subscription"}: verify_before_effect(db,row,inputs,bill_rows)
            if node["id"]=="analyze":
                evidence=duplicate_evidence(bill_rows,inputs.get("bill_source","sandbox"))
                from backend.banking_mission_evidence import capture
                capture(db,row,inputs,bill_rows,evidence)
                db.execute("UPDATE banking_missions SET evidence_json=? WHERE id=?",(json.dumps(evidence,ensure_ascii=False),mid))
                node["output"]={"record_count":len(bill_rows),"anomaly_count":len(evidence)}
            elif node["id"]=="budget_check":
                amount=_amount_fen(inputs["amount"]); floor=0 if inputs["minimum_balance"]=="0.00" else _amount_fen(inputs["minimum_balance"])
                if _available(user)-amount<floor: raise ValueError("预算条件不满足，尚未预留资金或取消订阅；请取消后重新调整计划")
                node["output"]={"available_after":_money(_available(user)-amount),"minimum_balance":inputs["minimum_balance"],"note":"执行预留前会再次检查余额"}
            elif node["id"]=="risk_review":
                if json.loads(row["evidence_json"]) or inputs.get('replan_requires_review'):
                    handoff(db,row,"risk_review",("预算已按你确认的条件预留，订阅取消等待人工核对" if inputs.get("risk_policy")=="reserve_before_review" else "账单存在疑似重复扣费，需要人工核对后继续") if json.loads(row["evidence_json"]) else "来源变化后需重新完成独立复核")
                    return True
                node["output"]={"decision":"no_anomaly_found"}
            elif node["id"]=="reserve":
                amount=_amount_fen(inputs["amount"]); floor=0 if inputs["minimum_balance"]=="0.00" else _amount_fen(inputs["minimum_balance"])
                if _available(user)-amount<floor: raise ValueError("预留后可用余额低于生活费底线；未修改金额，等待你处理")
                db.execute("UPDATE sandbox_sessions SET reserved_fen=reserved_fen+? WHERE id=?",(amount,sid))
                node["output"]={"reserved":inputs["amount"],"available_after":_money(_available(user)-amount),"minimum_balance":inputs["minimum_balance"]}
            elif node["id"]=="cancel_subscription":
                item=db.execute("SELECT * FROM sandbox_subscriptions WHERE id=? AND session_id=?",(inputs["subscription_id"],sid)).fetchone()
                if not item: raise ValueError("指定订阅已不可用")
                db.execute("UPDATE sandbox_subscriptions SET status='CANCELLED',updated_at=? WHERE id=?",(_now().isoformat(),item["id"]))
                node["output"]={"merchant":item["merchant"],"status":"CANCELLED","previous_status":item["status"],"sandbox":True}
            elif node["id"]=="notify":
                node["output"]={"message":"任务已完成，可核对每一步的执行结果与证据。"}
            node["status"]="COMPLETED"
            status="COMPLETED" if all(n["status"]=="COMPLETED" for n in nodes) else "RUNNING"
            db.execute("UPDATE banking_missions SET nodes_json=?,status=?,reason=NULL,updated_at=? WHERE id=?",(json.dumps(nodes,ensure_ascii=False),status,_now().isoformat(),mid))
            event(db,row,"NODE_CHECKPOINT",{"node_id":node["id"],"output":node["output"]})
        except ValueError as exc:
            db.execute("UPDATE banking_missions SET status='BLOCKED',reason=?,updated_at=? WHERE id=?",(str(exc),_now().isoformat(),mid))
            event(db,row,"NODE_BLOCKED",{"node_id":node["id"] if node else None,"reason":str(exc)})
        return True

def run_tick():
    with _connect() as db:
        schema(db); ids=[x['id'] for x in db.execute("SELECT id FROM banking_missions WHERE status='RUNNING' AND (not_before IS NULL OR not_before<=?) ORDER BY updated_at LIMIT 100",(_now().astimezone(timezone.utc).isoformat(),))]
    for mid in ids: tick(mid)

def operator(token,permission='review.read'):
    from backend.banking_identity import require
    return require(token,permission)

@router.get("/operator/reviews")
def inbox(x_banking_operator: str|None=Header(default=None),mission_id: str|None=None):
    operator(x_banking_operator)
    if mission_id is not None and (not mission_id or len(mission_id)>64):raise HTTPException(422,'任务编号无效')
    with _connect() as db:
        schema(db)
        return {"items":[{"review":dict(r),"mission":view(db,db.execute("SELECT * FROM banking_missions WHERE id=?",(r["mission_id"],)).fetchone())} for r in db.execute("SELECT * FROM banking_reviews WHERE status='PENDING' AND (? IS NULL OR mission_id=?) ORDER BY created_at LIMIT 100",(mission_id,mission_id))]}

@router.post("/operator/reviews/{rid}/decision")
def decide(rid: str,req: ReviewDecision,x_banking_operator: str|None=Header(default=None)):
    actor=operator(x_banking_operator,'review.decide')
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE")
        review=db.execute("SELECT * FROM banking_reviews WHERE id=? AND status='PENDING'",(rid,)).fetchone()
        if not review: raise HTTPException(409,"复核记录已处理")
        row=db.execute("SELECT * FROM banking_missions WHERE id=?",(review["mission_id"],)).fetchone()
        if row["status"]!="WAITING_REVIEW": raise HTTPException(409,"任务状态已变化")
        nodes=json.loads(row["nodes_json"])
        if req.decision=="approve":
            from backend.banking_mission_evidence import verify_before_effect, read_rows
            inputs=json.loads(row['input_json'])
            saved=db.execute('SELECT content_hash FROM banking_mission_sources WHERE mission_id=?',(row['id'],)).fetchone()
            if not saved or req.evidence_digest!=saved['content_hash']: raise HTTPException(409,'请先查阅当前证据，并提交对应快照摘要')
            try: verify_before_effect(db,row,inputs,read_rows(db,row['session_id'],inputs))
            except ValueError as error: raise HTTPException(409,str(error)) from None
        if req.decision=="approve" and review["node_id"]=="risk_review":
            node=next(n for n in nodes if n["id"]=="risk_review"); node.update(status="COMPLETED",output={"decision":"human_approved","note":req.note})
        db.execute("UPDATE banking_reviews SET status=?,decision_note=?,decided_at=? WHERE id=?",("APPROVED" if req.decision=="approve" else "REJECTED",req.note,_now().isoformat(),rid))
        db.execute("UPDATE banking_missions SET status=?,nodes_json=?,reason=?,updated_at=? WHERE id=?",("PAUSED" if req.decision=="approve" else "BLOCKED",json.dumps(nodes,ensure_ascii=False),"复核已批准，请由用户恢复任务" if req.decision=="approve" else "人工复核未通过",_now().isoformat(),row["id"]))
        event(db,row,"HUMAN_REVIEW_DECIDED",{"decision":req.decision,"note":req.note,"actor":actor['id'],"role":actor['role']})
        return view(db,db.execute("SELECT * FROM banking_missions WHERE id=?",(row["id"],)).fetchone())

@router.get("/conversation")
def conversation(x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db); user=_session(db,x_banking_session)
        return {"items":[{**dict(r),"attachment":json.loads(r["attachment"]) if r["attachment"] else None} for r in db.execute("SELECT id,role,text,attachment,created_at FROM banking_messages WHERE session_id=? ORDER BY id LIMIT 200",(user["id"],))]}


@router.get('/missions/{mid}')
def get_mission(mid:str,x_banking_session:str|None=Header(default=None)):
    with _connect() as db:
        schema(db);user=_session(db,x_banking_session)
        return view(db,owned(db,user['id'],mid))

@router.post("/conversation/messages")
def message(req: MessageRequest,x_banking_session: str|None=Header(default=None)):
    if bool(req.goal_id)!=bool(req.goal_digest):raise HTTPException(422,'修改目标需要同时提供草案编号与当前版本摘要')
    # Reserve the message ID before any draft creation, preventing concurrent replay.
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session)
        prior=db.execute("SELECT * FROM banking_messages WHERE session_id=? AND client_id=? AND role='agent'",(user["id"],req.client_id)).fetchone()
        if prior: return {"reply":prior["text"],"attachment":json.loads(prior["attachment"]) if prior["attachment"] else None}
        if db.execute("SELECT 1 FROM banking_messages WHERE session_id=? AND client_id=?",(user["id"],req.client_id)).fetchone(): raise HTTPException(409,"此消息正在处理中，请稍后刷新对话")
        db.execute("INSERT INTO banking_messages(session_id,client_id,role,text,created_at) VALUES(?,?,'user',?,?)",(user["id"],req.client_id,req.text,_now().isoformat()))
    attachment=None
    from backend import banking_goal_planner as goal_planner
    if req.goal_id:
        try:
            proposal=goal_planner.refine(req.goal_id,goal_planner.Refine(digest=req.goal_digest,text=req.text),x_banking_session)
            reply=f"已更新第 {proposal['revision']} 版草案，仅修改明确提及的条件，其他条件保留。请核对最新内容；本次修改没有授权执行。"
        except HTTPException as error:reply=str(error.detail)
        attachment={'kind':'goal_plan','goal_id':req.goal_id}
    elif goal_planner.applies(req.text):
        proposal=goal_planner.prepare(goal_planner.GoalRequest(text=req.text),x_banking_session)
        attachment={'kind':'goal_plan','goal_id':proposal['id']}
        reply='已整理目标与条件草案。请逐项核对原文依据，补齐缺失条件后再创建执行计划；当前未授权资金或订阅操作。'
    else:
        intent=interpret_intent(IntentRequest(text=req.text),x_banking_session); reply=intent["reply"]; attachment=intent
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO banking_messages(session_id,client_id,role,text,attachment,created_at) VALUES(?,?,'agent',?,?,?)",(user["id"],req.client_id,reply,json.dumps(attachment,ensure_ascii=False) if attachment else None,_now().isoformat()))
    return {"reply":reply,"attachment":attachment}
