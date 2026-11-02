"""Exact LEI registry lookup. Public entity records never authorize a payment."""
import hashlib
import json
import re
from datetime import datetime,timedelta
import httpx
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from backend import banking as b

BASE='https://api.gleif.org/api/v1/lei-records/'


class Lookup(BaseModel):
    lei: str=Field(min_length=20,max_length=20)
    confirmed: bool


def parse_record(raw,lei):
    data=raw['data']; attributes=data['attributes']; entity=attributes['entity']; registration=attributes['registration']
    if data['id']!=lei or attributes['lei']!=lei: raise ValueError('identifier mismatch')
    name=entity['legalName']['name']
    if not isinstance(name,str) or not 1<=len(name)<=500: raise ValueError('invalid entity name')
    return {'lei':lei,'name':name,'jurisdiction':entity.get('jurisdiction',''),'entity_status':entity.get('status','UNKNOWN'),'registration_status':registration.get('status','UNKNOWN'),'last_updated':registration.get('lastUpdateDate'),'next_renewal':registration.get('nextRenewalDate'),'source_url':BASE+lei,'source':'GLEIF','scope':'机构登记资料；不证明金融牌照、收款账户归属或交易安全。'}


def cached(db,lei):
    row=db.execute('SELECT * FROM sandbox_public_feeds WHERE source=?',('gleif:'+lei,)).fetchone()
    if not row or not row['payload']: return None
    try:
        if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['content_hash']: return None
        result=json.loads(row['payload'])
        result.update(fetched_at=row['fetched_at'],status='stale' if b._now()-datetime.fromisoformat(row['fetched_at'])>timedelta(hours=24) else 'cached')
        return result
    except (ValueError,TypeError): return None


@b.router.post('/external/entities/lookup')
def lookup(req: Lookup,x_banking_session: str|None=Header(default=None)):
    lei=req.lei.strip().upper()
    if not req.confirmed or not re.fullmatch('[A-Z0-9]{18}[0-9]{2}',lei): raise HTTPException(422,'请确认查询，并填写完整的20位 LEI 编号')
    # ISO 17442 uses ISO 7064 MOD 97-10. Reject typos before any external call.
    if int(''.join(str(ord(c)-55) if c.isalpha() else c for c in lei))%97!=1: raise HTTPException(422,'LEI 校验位不正确，请核对原始编号')
    now=b._now().isoformat()
    with b._connect() as db:
        db.execute('BEGIN IMMEDIATE'); user=b._session(db,x_banking_session)
        previous=cached(db,lei)
        if previous and previous['status']=='cached': return previous
        rate=db.execute('SELECT attempted_at FROM sandbox_public_feeds WHERE source=?',('gleif-rate:'+user['id'],)).fetchone()
        if rate and rate[0]>(b._now()-timedelta(seconds=10)).isoformat(): raise HTTPException(429,'请稍候十秒再查询机构来源')
        global_rate=db.execute("SELECT attempted_at FROM sandbox_public_feeds WHERE source='gleif-global-rate'").fetchone()
        if global_rate and global_rate[0]>(b._now()-timedelta(seconds=2)).isoformat(): raise HTTPException(429,'机构来源正在处理其他查询，请稍后重试')
        db.execute("INSERT INTO sandbox_public_feeds(source,attempted_at) VALUES('gleif-global-rate',?) ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at",(now,))
        db.execute('INSERT INTO sandbox_public_feeds(source,attempted_at) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at',('gleif-rate:'+user['id'],now))
    try:
        with httpx.Client(timeout=12,follow_redirects=False) as client:
            response=client.get(BASE+lei)
            if response.status_code==404: raise HTTPException(404,'GLEIF 未找到该编号；未找到不构成风险判断')
            response.raise_for_status()
            if len(response.content)>1_000_000: raise ValueError('oversize registry response')
            result=parse_record(response.json(),lei)
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        if previous: return {**previous,'status':'stale','warning':'官方接口暂不可用，保留历史快照'}
        raise HTTPException(503,'机构登记来源暂不可用，请稍后重试') from None
    payload=json.dumps(result,ensure_ascii=False,separators=(',',':'))
    with b._connect() as db:
        b._session(db,x_banking_session)
        db.execute('INSERT INTO sandbox_public_feeds(source,payload,content_hash,fetched_at,attempted_at) VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,content_hash=excluded.content_hash,fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at',('gleif:'+lei,payload,hashlib.sha256(payload.encode()).hexdigest(),now,now))
        b._audit(db,user['id'],None,'PUBLIC_ENTITY_LOOKUP',{'lei':lei,'source':'GLEIF'})
        db.execute("DELETE FROM sandbox_public_feeds WHERE source IN (SELECT source FROM sandbox_public_feeds WHERE source LIKE 'gleif:%' ORDER BY fetched_at DESC LIMIT -1 OFFSET 128)")
    return {**result,'status':'fresh','fetched_at':now}
