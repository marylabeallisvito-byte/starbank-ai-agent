"""Named local workforce identities; customer sessions never grant workforce roles."""
import hashlib
import os
import secrets
from datetime import timedelta
from fastapi import Header,HTTPException,Request,Response
from backend import banking as b

ROLES={'viewer':{'review.read'},'reviewer':{'review.read','review.decide'},'auditor':{'audit.read'},'admin':{'review.read','review.decide','audit.read','identity.manage'}}


def schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS banking_operator_identities(
    id TEXT PRIMARY KEY,display_name TEXT NOT NULL,role TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS banking_operator_sessions(
    token_hash TEXT PRIMARY KEY,operator_id TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(operator_id) REFERENCES banking_operator_identities(id));
    CREATE TABLE IF NOT EXISTS banking_operator_login_limits(
    client_hash TEXT PRIMARY KEY,window_start TEXT NOT NULL,attempts INTEGER NOT NULL);''')
    from backend.banking_storage import install
    install(db)
    legacy=os.getenv('BANKING_OPERATOR_TOKEN','')
    if len(legacy)>=24:
        db.execute("INSERT OR IGNORE INTO banking_operator_identities(id,display_name,role,token_hash,created_at) VALUES('local-reviewer','本机复核员','reviewer',?,?)",(hashlib.sha256(legacy.encode()).hexdigest(),b._now().isoformat()))


def require(token,permission):
    if not token or not 24<=len(token)<=200: raise HTTPException(403,'需要有效的独立工作台凭据')
    with b._connect() as db:
        schema(db)
        digest=hashlib.sha256(token.encode()).hexdigest()
        row=db.execute('SELECT * FROM banking_operator_identities WHERE token_hash=? AND enabled=1',(digest,)).fetchone()
        if not row:
            row=db.execute('''SELECT i.* FROM banking_operator_sessions s JOIN banking_operator_identities i ON i.id=s.operator_id
                WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>? AND i.enabled=1''',(digest,b._now().isoformat())).fetchone()
        if not row or permission not in ROLES.get(row['role'],set()): raise HTTPException(403,'当前身份没有此操作权限，或凭据已撤销')
        return {'id':row['id'],'display_name':row['display_name'],'role':row['role'],'permissions':sorted(ROLES[row['role']])}


def start_workforce_session(token,client):
    """Exchange an existing credential; never creates or expands workforce permissions."""
    client_hash=hashlib.sha256(client.encode()).hexdigest();now=b._now()
    with b._connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE')
        limit=db.execute('SELECT * FROM banking_operator_login_limits WHERE client_hash=?',(client_hash,)).fetchone()
        fresh=not limit or limit['window_start']<=(now-timedelta(minutes=1)).isoformat()
        attempts=1 if fresh else limit['attempts']+1
        if attempts>10:raise HTTPException(429,'登录尝试过于频繁，请一分钟后重试')
        db.execute('INSERT OR REPLACE INTO banking_operator_login_limits VALUES(?,?,?)',(client_hash,now.isoformat() if fresh else limit['window_start'],attempts));db.commit()
        if not token or not 24<=len(token)<=200:raise HTTPException(403,'凭据格式不正确；请只输入令牌内容，不含变量名、等号或引号')
        row=db.execute('SELECT * FROM banking_operator_identities WHERE token_hash=? AND enabled=1',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row:raise HTTPException(403,'凭据未通过校验，请检查复制内容；管理员可核查身份是否已撤销或凭据已轮换')
        if 'review.read' not in ROLES.get(row['role'],set()):raise HTTPException(403,'当前身份没有复核工作台读取权限，请使用复核员或只读查看身份')
        session=secrets.token_urlsafe(36);expires=now+timedelta(minutes=15)
        db.execute('DELETE FROM banking_operator_sessions WHERE expires_at<=? OR revoked=1',(now.isoformat(),))
        db.execute('INSERT INTO banking_operator_sessions(token_hash,operator_id,created_at,expires_at) VALUES(?,?,?,?)',(hashlib.sha256(session.encode()).hexdigest(),row['id'],now.isoformat(),expires.isoformat()))
        return {'session_token':session,'expires_at':expires.isoformat(),'identity':{'id':row['id'],'display_name':row['display_name'],'role':row['role'],'permissions':sorted(ROLES[row['role']])}}


@b.router.post('/operator/login')
def login(request:Request,response:Response,x_banking_operator:str|None=Header(default=None)):
    response.headers['Cache-Control']='no-store'
    return start_workforce_session(x_banking_operator,request.client.host if request.client else 'unknown')


@b.router.post('/operator/logout')
def logout(x_banking_operator:str|None=Header(default=None)):
    if x_banking_operator:
        with b._connect() as db:
            schema(db)
            db.execute('UPDATE banking_operator_sessions SET revoked=1 WHERE token_hash=?',(hashlib.sha256(x_banking_operator.encode()).hexdigest(),))
    return {'signed_out':True}


def create(db,identifier,name,role):
    if role not in ROLES or not identifier or len(identifier)>64 or not name or len(name)>100: raise ValueError('身份编号、名称或角色无效')
    token=secrets.token_urlsafe(36)
    db.execute('INSERT INTO banking_operator_identities(id,display_name,role,token_hash,created_at) VALUES(?,?,?,?,?)',(identifier,name,role,hashlib.sha256(token.encode()).hexdigest(),b._now().isoformat()))
    return token


@b.router.get('/operator/identity')
def identity(x_banking_operator: str|None=Header(default=None)):
    # Read-only identity discovery also works for an auditor who cannot enter the review queue.
    for permission in ('review.read','audit.read'):
        try:return require(x_banking_operator,permission)
        except HTTPException:pass
    raise HTTPException(403,'工作台凭据无效、已撤销或没有读取权限')


@b.router.get('/operator/audit')
def audit(x_banking_operator: str|None=Header(default=None)):
    actor=require(x_banking_operator,'audit.read')
    with b._connect() as db:
        return {'reader':actor,'events':[dict(row) for row in db.execute('SELECT event,created_at,detail,event_hash FROM sandbox_audit ORDER BY id DESC LIMIT 100')]}
