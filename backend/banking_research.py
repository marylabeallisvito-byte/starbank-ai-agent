"""Consented 3–7-person usability pilot; developer pilots never count as users."""
import hashlib
import json
import secrets
import statistics
from datetime import datetime
from typing import Literal
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from backend import banking as b

VERSION='usability-pilot-v1'
MONTHLY_VERSION='monthly-usability-v2'
TASKS=[{'id':'balance','instruction':'查询当前虚构账户的可用余额。'},
       {'id':'duplicate','instruction':'找到本月疑似重复扣费记录，核对商户与金额。'},
       {'id':'transfer_preview','instruction':'为妈妈准备 {amount} 元转账预览，核对收款人和金额。只停留在预览，不确认执行。'}]
MONTHLY_TASKS=[
    {'id':'monthly_preview','instruction':'整理本月账单，预留500元旅行预算，至少保留1000元生活费，取消云上视频订阅，遇到重复扣费先暂停。核对草案中的条件，只到草案，不启用。','question':'仅生成并修改草案，是否已经授权预留资金？','answer':'no'},
    {'id':'monthly_revision','instruction':'先整理一份预留500元旅行预算、至少保留1000元生活费、取消云上视频的账单安排草案。再将旅行预算减少200元，生活费不变，并撤回取消订阅。核对最终预算、底线和订阅范围，不启用。','question':'只改预算时，未明确修改的生活费底线是否应保留？','answer':'yes'},
    {'id':'monthly_review','instruction':'创建并启用本月账单、500元预算、1000元生活费底线与取消云上视频的安排，异常策略保持全部暂停。进入等待复核后，说明由谁处理、预算是否已预留；只查看，不自行批准。','question':'客户的普通会话是否可以代替独立复核员批准？','answer':'no'},
    {'id':'monthly_resume','instruction':'在新的虚构账户创建并启用同样的500元预算、1000元底线和云上视频安排。等待复核后，请主持人按协议使用独立复核身份核对并批准；你返回客户任务，自行恢复办理，再释放预算，核对现金和订阅状态。','question':'复核员批准后，后续资金和订阅步骤是否已经自动办理？','answer':'no'},
]


def task_count(protocol):return 4 if protocol==MONTHLY_VERSION else 6


def schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS banking_research_people(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,code_hash TEXT UNIQUE NOT NULL,cohort TEXT NOT NULL,arm_order TEXT NOT NULL,consented_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS banking_research_trials(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,sequence INTEGER NOT NULL,arm TEXT NOT NULL,task_id TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT,payload TEXT,UNIQUE(person_id,sequence));''')
    if 'protocol' not in {row['name'] for row in db.execute('PRAGMA table_info(banking_research_people)')}:
        db.execute("ALTER TABLE banking_research_people ADD COLUMN protocol TEXT NOT NULL DEFAULT 'usability-pilot-v1'")
    from backend.banking_storage import install
    install(db)


class Consent(BaseModel):
    protocol:Literal['usability-pilot-v1','monthly-usability-v2']=VERSION
    participant_code:str=Field(pattern=r'^[A-Za-z0-9-]{3,20}$')
    cohort:Literal['developer_pilot','external_participant']
    confirmed:bool


class Response(BaseModel):
    trial_id:str=Field(max_length=64)
    outcome:Literal['success','failed','stopped']
    ease:int=Field(ge=1,le=7)
    confidence:int=Field(ge=1,le=7)
    comment:str=Field(default='',max_length=500)
    comprehension:Literal['yes','no','unsure']|None=None
    assistance:Literal['none','protocol_reviewer','extra_help']='none'


def person(db,token):
    if not token or len(token)>200:raise HTTPException(401,'需要当前研究会话')
    row=db.execute('SELECT * FROM banking_research_people WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row:raise HTTPException(401,'研究会话无效')
    return row


def trial_view(row):
    if not row:return None
    task=next(t for t in TASKS+MONTHLY_TASKS if t['id']==row['task_id'])
    return {'id':row['id'],'sequence':row['sequence'],'arm':row['arm'],'task_id':row['task_id'],'instruction':task['instruction'].format(amount='400' if row['sequence']<3 else '800'),
            'started_at':row['started_at'],'comprehension_question':task.get('question'),'reviewer_step':row['task_id']=='monthly_resume','path':'/banking/classic?research_fresh=1' if row['arm']=='menu' else '/banking?research_fresh=1'}


@b.router.post('/research/enroll')
def enroll(req:Consent):
    if not req.confirmed:raise HTTPException(422,'请先阅读并明确同意研究说明')
    from backend.banking_storage import keys
    if not keys():raise HTTPException(503,'研究记录收集前必须先启用本机敏感字段加密')
    token=secrets.token_urlsafe(32)
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE')
        code=hashlib.sha256((req.protocol+req.participant_code.upper()).encode()).hexdigest()
        if db.execute('SELECT 1 FROM banking_research_people WHERE code_hash=?',(code,)).fetchone():raise HTTPException(409,'该参与编号已使用，请恢复原研究会话或联系主持人')
        count=db.execute('SELECT COUNT(*) FROM banking_research_people WHERE cohort=? AND protocol=?',(req.cohort,req.protocol)).fetchone()[0]
        if count>=50:raise HTTPException(409,'本地试点已达到会话上限')
        order='agent' if req.protocol==MONTHLY_VERSION else 'menu,agent' if count%2==0 else 'agent,menu'
        db.execute('INSERT INTO banking_research_people(id,token_hash,code_hash,cohort,arm_order,consented_at,protocol) VALUES(?,?,?,?,?,?,?)',(secrets.token_hex(12),hashlib.sha256(token.encode()).hexdigest(),code,req.cohort,order,b._now().isoformat(),req.protocol))
    return {'token':token,'protocol_version':req.protocol,'arm_order':order.split(','),'task_count':task_count(req.protocol),'cohort':req.cohort}


@b.router.get('/research/session')
def session(x_research_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);p=person(db,x_research_session)
        completed=db.execute('SELECT COUNT(*) FROM banking_research_trials WHERE person_id=? AND completed_at IS NOT NULL',(p['id'],)).fetchone()[0]
        active=db.execute('SELECT * FROM banking_research_trials WHERE person_id=? AND completed_at IS NULL',(p['id'],)).fetchone()
        return {'completed':completed,'total':task_count(p['protocol']),'protocol_version':p['protocol'],'active':trial_view(active),'cohort':p['cohort'],'arm_order':p['arm_order'].split(',')}


@b.router.post('/research/trials/start')
def start(x_research_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');p=person(db,x_research_session)
        active=db.execute('SELECT * FROM banking_research_trials WHERE person_id=? AND completed_at IS NULL',(p['id'],)).fetchone()
        if active:return trial_view(active)
        index=db.execute('SELECT COUNT(*) FROM banking_research_trials WHERE person_id=?',(p['id'],)).fetchone()[0]
        if index>=task_count(p['protocol']):raise HTTPException(409,'本轮研究任务已完成')
        identifier=secrets.token_hex(12)
        monthly=p['protocol']==MONTHLY_VERSION
        db.execute('INSERT INTO banking_research_trials(id,person_id,sequence,arm,task_id,started_at) VALUES(?,?,?,?,?,?)',(identifier,p['id'],index,'agent' if monthly else p['arm_order'].split(',')[index//3],MONTHLY_TASKS[index]['id'] if monthly else TASKS[index%3]['id'],b._now().isoformat()))
        return trial_view(db.execute('SELECT * FROM banking_research_trials WHERE id=?',(identifier,)).fetchone())


@b.router.post('/research/trials/complete')
def complete(req:Response,x_research_session:str|None=Header(default=None)):
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');p=person(db,x_research_session)
        row=db.execute('SELECT * FROM banking_research_trials WHERE id=? AND person_id=?',(req.trial_id,p['id'])).fetchone()
        if not row:raise HTTPException(404,'研究任务不存在')
        if row['completed_at']:raise HTTPException(409,'该任务结果已记录，请勿重复提交')
        monthly=p['protocol']==MONTHLY_VERSION
        if monthly and req.comprehension is None:raise HTTPException(422,'请回答当前任务的理解问题；不确定可以选择不确定')
        if row['task_id']=='monthly_resume' and req.outcome=='success' and req.assistance=='none':raise HTTPException(422,'恢复任务包含主持人的独立复核步骤，请准确记录本项帮助情况')
        elapsed=max(0,(b._now()-datetime.fromisoformat(row['started_at'])).total_seconds())
        payload={**req.model_dump(),'elapsed_seconds':round(elapsed,2),'success_source':'participant_self_report','timing_source':'server_wall_clock','include_timing':elapsed<=1800}
        if monthly:payload['comprehension_correct']=req.comprehension==next(t['answer'] for t in MONTHLY_TASKS if t['id']==row['task_id'])
        db.execute('UPDATE banking_research_trials SET completed_at=?,payload=? WHERE id=?',(b._now().isoformat(),json.dumps(payload,ensure_ascii=False),row['id']))
    return {'recorded':True,'elapsed_seconds':payload['elapsed_seconds'],'success_verified_by_server':False}


@b.router.get('/research/report')
def report(protocol:Literal['usability-pilot-v1','monthly-usability-v2']=VERSION):
    if protocol not in {VERSION,MONTHLY_VERSION}:raise HTTPException(422,'研究协议无效')
    with b._connect() as db:
        schema(db)
        counts={row['cohort']:row['n'] for row in db.execute('SELECT cohort,COUNT(*) n FROM banking_research_people WHERE protocol=? GROUP BY cohort',(protocol,))}
        people=[row['person_id'] for row in db.execute("SELECT t.person_id FROM banking_research_trials t JOIN banking_research_people p ON p.id=t.person_id WHERE p.cohort='external_participant' AND p.protocol=? AND t.completed_at IS NOT NULL GROUP BY t.person_id HAVING COUNT(*)=?",(protocol,task_count(protocol)))]
        groups={arm:[] for arm in (('agent',) if protocol==MONTHLY_VERSION else ('menu','agent'))}
        for pid in people:
            for arm in groups:
                rows=[json.loads(row['payload']) for row in db.execute('SELECT payload FROM banking_research_trials WHERE person_id=? AND arm=? AND completed_at IS NOT NULL',(pid,arm))]
                groups[arm].append({'success_rate':sum(r['outcome']=='success' for r in rows)/len(rows),'ease':statistics.mean(r['ease'] for r in rows),'comprehension':sum(r.get('comprehension_correct',False) for r in rows)/len(rows),'extra_help':sum(r.get('assistance')=='extra_help' for r in rows)/len(rows),'duration':statistics.median([r['elapsed_seconds'] for r in rows if r['include_timing']]) if any(r['include_timing'] for r in rows) else None})
    metrics=None
    if len(people)>=3:
        metrics={arm:{'self_reported_success_rate':round(statistics.mean(p['success_rate'] for p in values),3),'median_participant_ease':statistics.median(p['ease'] for p in values),'median_participant_duration_seconds':statistics.median(p['duration'] for p in values if p['duration'] is not None) if any(p['duration'] is not None for p in values) else None} for arm,values in groups.items()}
        if protocol==MONTHLY_VERSION:
            metrics['agent'].update(comprehension_answer_accuracy=round(statistics.mean(p['comprehension'] for p in groups['agent']),3),extra_help_trial_rate=round(statistics.mean(p['extra_help'] for p in groups['agent']),3))
    return {'protocol_version':protocol,'comparison_design':'single_arm_monthly_tasks' if protocol==MONTHLY_VERSION else 'counterbalanced_menu_agent','developer_pilot_sessions':counts.get('developer_pilot',0),'external_consented_sessions':counts.get('external_participant',0),'completed_external_sessions':len(people),'metrics':metrics,'descriptive_only':True,
            'limitations':['参与者类别由主持人与参与者申报，未核实真实身份。','开发者试跑不计入外部用户结果；少于三名完整参与者不展示指标。','完成情况为参与者自报，未自动验证任务正确性；计时为服务端经过时间。','3–7 人可用性试点不能证明总体效果，固定任务与学习效应需在报告中说明。']+(['月度协议为单组四项任务，无菜单效率对照；理解题正确不代表业务办理正确。','恢复任务按协议包含独立复核，经过时间包含等待；额外帮助单独申报。'] if protocol==MONTHLY_VERSION else [])}


class Withdraw(BaseModel):confirmed:bool


@b.router.post('/research/withdraw')
def withdraw(req:Withdraw,x_research_session:str|None=Header(default=None)):
    if not req.confirmed:raise HTTPException(422,'请确认清除研究记录')
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');p=person(db,x_research_session)
        db.execute('DELETE FROM banking_research_trials WHERE person_id=?',(p['id'],))
        db.execute('DELETE FROM banking_research_people WHERE id=?',(p['id'],))
    return {'cleared':True}
