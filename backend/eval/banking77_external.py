"""Frozen external-source route assessment; failures remain in the report."""
import argparse
import csv
import hashlib
import io
import json
import os
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import httpx
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[2]
PROTOCOL=Path(__file__).with_name('banking77_protocol_v1.json')
DATA=ROOT/'data/external/BANKING77/test.csv'


def source(protocol,download=False):
    if not DATA.is_file():
        if not download:raise ValueError('download the checksum-pinned public test CSV first')
        response=httpx.get(protocol['source_url'],timeout=30,follow_redirects=False);response.raise_for_status()
        if len(response.content)>1_000_000:raise ValueError('oversize dataset')
        if hashlib.sha256(response.content).hexdigest()!=protocol['source_sha256']:raise ValueError('Official test data changed; refuse to substitute another version')
        DATA.parent.mkdir(parents=True,exist_ok=True);DATA.write_bytes(response.content)
    raw=DATA.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=protocol['source_sha256']:raise ValueError('Local public dataset checksum mismatch')
    rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    if len(rows)!=3080 or not {'text','category'}<=rows[0].keys():raise ValueError('Unexpected official test schema')
    return rows


def run(live=False,download=False):
    load_dotenv(ROOT/'.env',override=False)
    from backend import banking as b
    from backend.banking_readiness import source_hash
    protocol=json.loads(PROTOCOL.read_text(encoding='utf-8'))
    rows=source(protocol,download)
    selected=[]
    for category,expected in protocol['mapping'].items():
        candidates=[r for r in rows if r['category']==category]
        candidates.sort(key=lambda r:hashlib.sha256((protocol['seed']+r['text']).encode()).hexdigest())
        if len(candidates)<protocol['per_category']:raise ValueError('Insufficient declared category examples')
        selected.extend({**r,'expected':expected} for r in candidates[:protocol['per_category']])
    results=[]
    directory=ROOT/'backend/data';directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temp,patch.object(b,'DB_PATH',Path(temp)/'external.sqlite3'),patch.dict(os.environ,{'BANKING_LLM_ENABLED':'1' if live else '0','BANKING_AUTH_MODE':'passkey'}):
        for index,item in enumerate(selected):
            token=b.create_session()['token'];start=time.perf_counter()
            actual=b.interpret_intent(b.IntentRequest(text=item['text']),token)
            results.append({**item,'case_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),'actual':actual['kind'],'pass':actual['kind']==item['expected'],'latency_ms':round((time.perf_counter()-start)*1000,2)})
            if (index+1)%10==0:print(json.dumps({'completed':index+1,'total':len(selected)}),flush=True)
        with b._connect() as db:
            no_funds=db.execute('SELECT COUNT(*) FROM sandbox_sessions WHERE balance_fen!=250000 OR reserved_fen!=0').fetchone()[0]==0
            action_counts={table:db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('sandbox_transfers','sandbox_card_actions','sandbox_wealth_orders')}
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'protocol':protocol,'protocol_sha256':hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'application_source_sha256':source_hash(),'live_model':live,'model':os.getenv('BANKING_LLM_MODEL',os.getenv('LLM_MODEL')) if live else None,'passed':sum(r['pass'] for r in results),'total':len(results),'cases':results,'by_category':{category:{'passed':sum(r['pass'] for r in results if r['category']==category),'total':sum(r['category']==category for r in results)} for category in protocol['mapping']},'no_funds_moved':no_funds,'action_counts':action_counts,'limitations':['Original English queries; this is a language-shift stress assessment, not Chinese product success rate.','Application label mapping is authored by this project, not by an independent assessor.','Public benchmark content may be present in model pretraining; no claim of model-unseen data.','Only four categories and forty preselected examples; not 77-class accuracy or end-to-end banking reliability.']}
    path=ROOT/'backend/eval/reports'/('banking77_live_v1.json' if live else 'banking77_rules_v1.json')
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8'))
        stamp=datetime.fromisoformat(old['generated_at']).strftime('%Y%m%dT%H%M%S%fZ')
        archive=path.with_name(path.stem+'_'+stamp+'.json')
        if not archive.exists():archive.write_bytes(path.read_bytes())
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(path),'passed':report['passed'],'total':report['total'],'no_funds_moved':no_funds,'by_category':report['by_category']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--download',action='store_true');parser.add_argument('--live-model',action='store_true')
    args=parser.parse_args();run(args.live_model,args.download)
