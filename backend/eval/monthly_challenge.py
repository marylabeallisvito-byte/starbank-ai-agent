"""Frozen new-task baseline. Keeps first results; never modifies business state in the main DB."""
import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import HTTPException
from backend import banking as b,banking_missions as m,banking_goal_planner as g

ROOT=Path(__file__).resolve().parents[2]
CORPUS=Path(__file__).with_name('monthly_challenge_v1.json')
PROTOCOL=Path(__file__).with_name('monthly_challenge_protocol_v1.json')
DIRECTORY=ROOT/'backend/eval/reports/monthly_challenge_v1'


def freeze():
    checksum=hashlib.sha256(CORPUS.read_bytes()).hexdigest()
    if PROTOCOL.exists():
        if json.loads(PROTOCOL.read_text(encoding='utf-8'))['corpus_sha256']!=checksum:raise RuntimeError('Frozen corpus changed; create a new version instead')
    else:
        with PROTOCOL.open('x',encoding='utf-8') as output:json.dump({'version':'monthly-challenge-protocol-v1','frozen_at':datetime.now(timezone.utc).isoformat(),'corpus_sha256':checksum,'authorship':'project_developer','independent':False,'first_run_immutable':True},output,ensure_ascii=False,indent=2)
    return checksum


def run(live_model=False):
    checksum=freeze();corpus=json.loads(CORPUS.read_text(encoding='utf-8'));rows=[]
    env={'BANKING_DATA_ENCRYPTION_KEY':Fernet.generate_key().decode(),'BANKING_DATA_PREVIOUS_KEYS':'','BANKING_LLM_ENABLED':'1' if live_model else '0','BANKING_AUTH_MODE':'demo'}
    with tempfile.TemporaryDirectory(dir=ROOT/'backend/data') as folder,patch.object(b,'DB_PATH',Path(folder)/'challenge.sqlite3'),patch.dict(os.environ,env):
        for index,case in enumerate(corpus['cases']):
            token=b.create_session()['token'];start=time.perf_counter();actual={};error=None;original=None;violation=False
            try:
                if case['kind']=='prepare':
                    answer=m.message(m.MessageRequest(text=case['text'],client_id=f'challenge-prepare-{index}'),token)
                    attachment=answer.get('attachment') or {}
                    if attachment.get('kind')=='goal_plan':
                        value=g.read(attachment['goal_id'],token);actual={'status':value['status'],**{key:value['plan'].get(key) for key in ('amount','minimum_balance','cancel_subscription')}}
                        actual['questions']=sorted(item['field'] for item in value['questions'])
                    else:actual={'kind':attachment.get('kind'),'status':'NOT_A_GOAL'}
                elif case['kind']=='refine':
                    original=g.prepare(g.GoalRequest(text=corpus['base']),token)
                    value=g.refine(original['id'],g.Refine(digest=original['digest'],text=case['text']),token)
                    actual={key:value['plan'].get(key) for key in ('amount','minimum_balance','cancel_subscription','not_before')}
                    actual['revision']=value['revision']
                else:
                    answer=b.interpret_intent(b.IntentRequest(text=case['text']),token)
                    actual={key:answer.get(key) for key in ('kind','recipient_id','amount')}
                    if actual.get('amount') is not None:actual['amount']=b._money(b._amount_fen(str(actual['amount'])))
            except HTTPException as failure:error={'status':failure.status_code,'message':str(failure.detail)}
            if case.get('reject'):
                current=g.read(original['id'],token) if original else None
                passed=error is not None and current is not None and current['digest']==original['digest']
                violation=current is not None and current['digest']!=original['digest']
            else:passed=error is None and all(actual.get(key)==value for key,value in case.get('expect',{}).items()) and all(field in actual.get('questions',[]) for field in case.get('questions',[]))
            with b._connect() as db:
                money=b.overview(token)
                changed=money['balance']!='2500.00' or money['reserved']!='0.00'
                changed=changed or db.execute("SELECT COUNT(*) FROM sandbox_subscriptions WHERE session_id=(SELECT id FROM sandbox_sessions WHERE token_hash=?) AND status!='ACTIVE'",(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0]>0
            rows.append({**case,'pass':passed and not changed,'actual':actual,'error':error,'unexpected_money_or_subscription_effect':bool(changed),'rejected_edit_mutated':violation,'latency_ms':round((time.perf_counter()-start)*1000,2)})
    from backend.banking_readiness import source_hash
    result={'version':corpus['version'],'created_at':datetime.now(timezone.utc).isoformat(),'corpus_sha256':checksum,'source_sha256':source_hash(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'evaluation_use':'development_retest' if (DIRECTORY/'first_run.json').exists() else 'first_new_task_run','live_model':live_model,'scope':corpus['scope'],'passed':sum(row['pass'] for row in rows),'total':len(rows),'unexpected_effects':sum(row['unexpected_money_or_subscription_effect'] or row['rejected_edit_mutated'] for row in rows),'valid_requests_rejected':sum(bool(row['error']) for row in rows if not row.get('reject')),'rows':rows,'limitations':['Developer-authored new-task challenge, not independent or statistically blind.','Only monthly preparation, revision and intent routing are covered; no real bank or device verification.','Latency is direct backend-call latency, not UI task time.']}
    DIRECTORY.mkdir(parents=True,exist_ok=True)
    path=DIRECTORY/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x',encoding='utf-8') as output:json.dump(result,output,ensure_ascii=False,indent=2)
    first=DIRECTORY/'first_run.json'
    if not first.exists():
        with first.open('x',encoding='utf-8') as output:json.dump(result,output,ensure_ascii=False,indent=2)
    return result,path


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--freeze',action='store_true');parser.add_argument('--live-model',action='store_true');args=parser.parse_args()
    load_dotenv(ROOT/'.env',override=False)
    if args.freeze:print(json.dumps({'corpus_sha256':freeze(),'frozen':True}))
    else:
        result,path=run(args.live_model);print(json.dumps({'report':str(path),'passed':result['passed'],'total':result['total'],'unexpected_effects':result['unexpected_effects']}));raise SystemExit(0 if result['passed']==result['total'] else 1)
