"""Authenticated field encryption for local sensitive text; keys stay outside SQLite.

Ledger integers, identifiers and indexes remain searchable. This is not whole-file
encryption or protection from a process that can read the service's key material.
"""
import hashlib
import os
import sqlite3
from functools import lru_cache
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

PREFIX='~BANKING-AE1~'
FIELDS={
    'banking_goal_proposals':('payload',),
    'sandbox_wealth_profiles':('answers_json',),
    'banking_research_trials':('payload',),
    'banking_operator_identities':('display_name',),
    'statement_drafts':('filename','rows_json','issues_json'),
    'statement_records':('payload',),
    'banking_missions':('title','input_json','nodes_json','evidence_json','reason'),
    'banking_mission_events':('detail',),
    'banking_mission_sources':('payload',),
    'banking_reviews':('reason','decision_note'),
    'banking_messages':('text','attachment'),
    'sandbox_audit':('detail',),
    'sandbox_transfers':('note','risk_snapshot'),
    'sandbox_dialog_context':('slots_json',),
    'sandbox_plans':('title','nodes_json','failure_reason'),
    'sandbox_notifications':('message',),
    'sandbox_plaid_connections':('accounts_json',),
    'sandbox_plaid_transactions':('payload',),
    'fx_budget_quotes':('payload',),
}


@lru_cache(maxsize=4)
def _keys(active,previous):
    values=[v.strip() for v in [active,*previous.split(',')] if v.strip()]
    try: return [(hashlib.sha256(v.encode()).hexdigest()[:12],Fernet(v.encode())) for v in dict.fromkeys(values)]
    except (ValueError,TypeError): raise HTTPException(503,'本机数据加密密钥格式无效，请修复服务端配置') from None


def keys(): return _keys(os.getenv('BANKING_DATA_ENCRYPTION_KEY',''),os.getenv('BANKING_DATA_PREVIOUS_KEYS',''))


def open_value(value):
    if not isinstance(value,str) or not value.startswith(PREFIX): return value
    try:
        kid,token=value[len(PREFIX):].split(':',1)
        for identifier,cipher in keys():
            if identifier==kid: return cipher.decrypt(token.encode()).decode('utf-8')
    except (ValueError,InvalidToken,UnicodeError): pass
    raise HTTPException(503,'敏感数据无法解密：密钥缺失、已轮换或数据损坏；已停止读取')


def seal(value):
    if value is None: return None
    ring=keys()
    if not ring: raise HTTPException(503,'敏感数据写入需要配置本机加密密钥')
    if isinstance(value,str) and value.startswith(PREFIX):
        plain=open_value(value)
        if value.startswith(PREFIX+ring[0][0]+':'): return value
    else: plain=str(value)
    kid,cipher=ring[0]
    return PREFIX+kid+':'+cipher.encrypt(plain.encode()).decode()


def raw(db,sql,params=()):
    cursor=db.cursor();cursor.row_factory=None
    return cursor.execute(sql,params).fetchall()


def row_factory(cursor,row): return sqlite3.Row(cursor,tuple(open_value(value) for value in row))


def configure(db):
    keys()
    db.row_factory=row_factory
    db.execute('PRAGMA secure_delete=ON')
    db.execute('PRAGMA recursive_triggers=OFF')
    db.create_function('bank_seal',1,seal)
    db.create_function('bank_needs_seal',1,lambda value:int(value is not None and (not isinstance(value,str) or not value.startswith(PREFIX+(keys()[0][0] if keys() else '')+':'))))
    if raw(db,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='banking_storage_meta'"):
        guard=raw(db,"SELECT value FROM banking_storage_meta WHERE key='guard'")
        if guard: open_value(guard[0][0])


def install(db):
    exists=raw(db,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='banking_storage_meta'")
    ring=keys()
    if exists:
        guard=raw(db,"SELECT value FROM banking_storage_meta WHERE key='guard'")
        if guard and open_value(guard[0][0])!='banking-storage-guard-v1': raise HTTPException(503,'数据加密配置校验失败')
    if not ring: return
    db.execute('CREATE TABLE IF NOT EXISTS banking_storage_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    todo=[]
    for table,columns in FIELDS.items():
        actual={r[1] for r in raw(db,f'PRAGMA table_info({table})')}
        fields=[c for c in columns if c in actual]
        if not fields: continue
        version=hashlib.sha256(','.join(fields).encode()).hexdigest()
        saved=raw(db,'SELECT value FROM banking_storage_meta WHERE key=?',('table:'+table,))
        if not saved or saved[0][0]!=version: todo.append((table,fields,version))
    guard=raw(db,"SELECT value FROM banking_storage_meta WHERE key='guard'")
    rotation=bool(guard and not guard[0][0].startswith(PREFIX+ring[0][0]+':'))
    if rotation:
        todo=[(table,[c for c in cols if c in {r[1] for r in raw(db,f'PRAGMA table_info({table})')}],hashlib.sha256(','.join(cols).encode()).hexdigest()) for table,cols in FIELDS.items() if raw(db,f'PRAGMA table_info({table})')]
    if todo or not guard:
        if db.in_transaction: raise HTTPException(503,'加密迁移需要独立事务，请在服务启动阶段执行')
        db.execute('BEGIN IMMEDIATE')
        try:
            had_rows=False
            for table,fields,version in todo:
                if not fields: continue
                assignments=','.join(f'{c}=bank_seal(NEW.{c})' for c in fields)
                condition=' OR '.join(f'bank_needs_seal(NEW.{c})' for c in fields)
                for action in ('INSERT','UPDATE'):
                    name=f'bank_enc_{table}_{action.lower()}'
                    db.execute(f'DROP TRIGGER IF EXISTS {name}')
                    db.execute(f'CREATE TRIGGER {name} AFTER {action} ON {table} WHEN {condition} BEGIN UPDATE {table} SET {assignments} WHERE rowid=NEW.rowid; END')
                had_rows|=bool(raw(db,f'SELECT 1 FROM {table} LIMIT 1'))
                setters=','.join(f'{c}=bank_seal({c})' for c in fields)
                db.execute(f'UPDATE {table} SET {setters}')
                db.execute('INSERT OR REPLACE INTO banking_storage_meta VALUES(?,?)',('table:'+table,version))
            db.execute("INSERT OR REPLACE INTO banking_storage_meta VALUES('guard',?)",(seal('banking-storage-guard-v1'),))
            if had_rows: db.execute("INSERT OR REPLACE INTO banking_storage_meta VALUES('needs_vacuum','1')")
            db.commit()
        except Exception: db.rollback();raise
    if raw(db,"SELECT 1 FROM banking_storage_meta WHERE key='needs_vacuum'"):
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('VACUUM')
        db.execute("DELETE FROM banking_storage_meta WHERE key='needs_vacuum'")


def status(db):
    available=bool(keys())
    return {'enabled':available,'active_key_id':keys()[0][0] if available else None,'previous_key_count':max(0,len(keys())-1),
            'scope':'敏感文本字段认证加密；账本数值、索引和标识符不属于整库加密。'}
