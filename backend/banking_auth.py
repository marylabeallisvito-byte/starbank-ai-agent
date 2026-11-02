"""WebAuthn user-verified transaction authorization, separate from model output."""
from __future__ import annotations
import hashlib
import json
import os
import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from webauthn import generate_registration_options, generate_authentication_options, verify_registration_response, verify_authentication_response, options_to_json, base64url_to_bytes
from webauthn.helpers.structs import AuthenticatorSelectionCriteria, UserVerificationRequirement, PublicKeyCredentialDescriptor
from backend.banking import _connect, _session, _now, _audit, _sandbox_enabled

router = APIRouter(prefix="/api/banking/auth", tags=["banking-auth"], dependencies=[Depends(_sandbox_enabled)])
TABLES = {"transfer": "sandbox_transfers", "card": "sandbox_card_actions", "wealth": "sandbox_wealth_orders", "subscription": "sandbox_subscription_actions"}

def mode() -> str:
    return "demo" if os.getenv("BANKING_AUTH_MODE") == "demo" and os.getenv("APP_ENV", "development") != "production" else "passkey"

def require_demo() -> None:
    if mode() != "demo":
        raise HTTPException(403, "当前要求设备通行密钥验证，演示验证码已关闭")

def schema(db) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS banking_passkeys(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,public_key BLOB NOT NULL,sign_count INTEGER NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS banking_auth_challenges(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,purpose TEXT NOT NULL,action_kind TEXT,action_id TEXT,digest TEXT,challenge TEXT NOT NULL,expires_at TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS banking_auth_grants(session_id TEXT NOT NULL,action_kind TEXT NOT NULL,action_id TEXT NOT NULL,digest TEXT NOT NULL,expires_at TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,action_kind,action_id));
    """)

def config():
    return os.getenv("BANKING_WEBAUTHN_RP_ID", "localhost"), os.getenv("BANKING_WEBAUTHN_ORIGIN", "http://localhost:5173")

def _challenge(db, sid, purpose, options, kind=None, action_id=None, digest=None):
    encoded = json.loads(options_to_json(options))
    cid = secrets.token_hex(16)
    db.execute("INSERT INTO banking_auth_challenges(id,session_id,purpose,action_kind,action_id,digest,challenge,expires_at) VALUES(?,?,?,?,?,?,?,?)", (cid,sid,purpose,kind,action_id,digest,encoded["challenge"],(_now()+timedelta(minutes=3)).isoformat()))
    return {"challenge_id": cid, "options": encoded}

class Verify(BaseModel):
    challenge_id: str = Field(max_length=64)
    credential: dict[str, Any]

class Action(BaseModel):
    kind: str
    action_id: str

def _owned_action(db, sid, kind, action_id):
    table = TABLES.get(kind)
    if not table:
        raise HTTPException(422, "不支持的验证对象")
    row = db.execute(f"SELECT * FROM {table} WHERE id=? AND session_id=?", (action_id,sid)).fetchone()
    if not row or row["status"] != "AWAITING_VERIFICATION" or row["expires_at"] <= _now().isoformat():
        raise HTTPException(409, "请先确认有效的红色操作预览")
    return row

@router.get("/status")
def status(x_banking_session: str | None = Header(default=None)):
    with _connect() as db:
        schema(db); user = _session(db,x_banking_session)
        return {"mode":mode(),"enrolled": bool(db.execute("SELECT 1 FROM banking_passkeys WHERE session_id=?",(user["id"],)).fetchone()),"rp_id":config()[0],"origin":config()[1],"identity_scope":"synthetic_session","sandbox":True}

@router.post("/register/options")
def registration_options(x_banking_session: str | None = Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session)
        if db.execute("SELECT 1 FROM banking_passkeys WHERE session_id=?",(user["id"],)).fetchone():
            raise HTTPException(409,"此沙箱会话已绑定设备，不允许通过会话令牌替换")
        if user["locked_until"] and user["locked_until"]>_now().isoformat(): raise HTTPException(423,"账户暂时锁定")
        options=generate_registration_options(rp_id=config()[0],rp_name="星河银行竞赛沙箱",user_id=user["id"].encode(),user_name="sandbox-"+user["id"][:8],authenticator_selection=AuthenticatorSelectionCriteria(user_verification=UserVerificationRequirement.REQUIRED),timeout=120000)
        return _challenge(db,user["id"],"register",options)

def _load_challenge(db,user,req,purpose):
    row=db.execute("SELECT * FROM banking_auth_challenges WHERE id=? AND session_id=? AND purpose=?",(req.challenge_id,user["id"],purpose)).fetchone()
    if not row or row["used"] or row["attempts"]>=3 or row["expires_at"]<=_now().isoformat(): raise HTTPException(409,"验证挑战已过期、已使用或被锁定")
    if user["locked_until"] and user["locked_until"]>_now().isoformat(): raise HTTPException(423,"账户暂时锁定")
    return row

def _failure(db,user,row):
    attempts=row["attempts"]+1
    db.execute("UPDATE banking_auth_challenges SET attempts=? WHERE id=?",(attempts,row["id"]))
    if attempts>=3: db.execute("UPDATE sandbox_sessions SET locked_until=? WHERE id=?",((_now()+timedelta(minutes=10)).isoformat(),user["id"]))
    _audit(db,user["id"],row["action_id"],"PASSKEY_REJECTED",{"attempts":attempts})
    db.commit()
    raise HTTPException(403,"设备签名验证失败")

@router.post("/register/verify")
def registration_verify(req: Verify,x_banking_session: str | None=Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session); row=_load_challenge(db,user,req,"register")
        if db.execute("SELECT 1 FROM banking_passkeys WHERE session_id=?",(user["id"],)).fetchone(): raise HTTPException(409,"设备已经绑定")
        try: result=verify_registration_response(credential=req.credential,expected_challenge=base64url_to_bytes(row["challenge"]),expected_rp_id=config()[0],expected_origin=config()[1],require_user_verification=True)
        except Exception: _failure(db,user,row)
        cid=req.credential["id"]
        db.execute("INSERT INTO banking_passkeys VALUES(?,?,?,?,?)",(cid,user["id"],result.credential_public_key,result.sign_count,_now().isoformat()))
        db.execute("UPDATE banking_auth_challenges SET used=1 WHERE id=?",(row["id"],))
        _audit(db,user["id"],None,"PASSKEY_ENROLLED",{"credential_hash":hashlib.sha256(result.credential_id).hexdigest(),"user_verified":True})
        return {"enrolled":True,"sandbox":True}

@router.post("/authorize/options")
def authorization_options(req: Action,x_banking_session: str | None=Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session); row=_owned_action(db,user["id"],req.kind,req.action_id)
        keys=db.execute("SELECT id FROM banking_passkeys WHERE session_id=?",(user["id"],)).fetchall()
        if not keys: raise HTTPException(409,"请先在安全中心绑定通行密钥")
        options=generate_authentication_options(rp_id=config()[0],allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(key["id"])) for key in keys],user_verification=UserVerificationRequirement.REQUIRED,timeout=120000)
        return _challenge(db,user["id"],"authorize",options,req.kind,req.action_id,row["digest"])

@router.get("/actions/{kind}/{action_id}")
def action_status(kind: str,action_id: str,x_banking_session: str | None=Header(default=None)):
    table=TABLES.get(kind)
    if not table: raise HTTPException(422,"不支持的操作")
    with _connect() as db:
        user=_session(db,x_banking_session)
        row=db.execute(f"SELECT status,digest FROM {table} WHERE id=? AND session_id=?",(action_id,user["id"])).fetchone()
        if not row: raise HTTPException(404,"操作不存在")
        return dict(row)

@router.post("/authorize/verify")
def authorization_verify(req: Verify,x_banking_session: str | None=Header(default=None)):
    with _connect() as db:
        schema(db); db.execute("BEGIN IMMEDIATE"); user=_session(db,x_banking_session); challenge=_load_challenge(db,user,req,"authorize")
        action=_owned_action(db,user["id"],challenge["action_kind"],challenge["action_id"])
        if not secrets.compare_digest(action["digest"],challenge["digest"]): raise HTTPException(409,"操作内容已变化")
        key=db.execute("SELECT * FROM banking_passkeys WHERE id=? AND session_id=?",(req.credential.get("id"),user["id"])).fetchone()
        if not key: _failure(db,user,challenge)
        try: result=verify_authentication_response(credential=req.credential,expected_challenge=base64url_to_bytes(challenge["challenge"]),expected_rp_id=config()[0],expected_origin=config()[1],credential_public_key=key["public_key"],credential_current_sign_count=key["sign_count"],require_user_verification=True)
        except Exception: _failure(db,user,challenge)
        now=_now().isoformat()
        db.execute("UPDATE banking_passkeys SET sign_count=? WHERE id=?",(result.new_sign_count,key["id"]))
        db.execute("UPDATE banking_auth_challenges SET used=1 WHERE id=?",(challenge["id"],))
        db.execute("INSERT OR REPLACE INTO banking_auth_grants VALUES(?,?,?,?,?,0)",(user["id"],challenge["action_kind"],action["id"],action["digest"],action["expires_at"]))
        db.execute(f"UPDATE {TABLES[challenge['action_kind']]} SET status='READY',verified_at=? WHERE id=?",(now,action["id"]))
        _audit(db,user["id"],action["id"],"PASSKEY_AUTHORIZED",{"kind":challenge["action_kind"],"digest":action["digest"],"user_verified":True})
        return {"verified":True,"sandbox":True}

def require_grant(db,sid,kind,row):
    if mode()=="demo": return
    # Schema is initialized by enrollment/authorization, never midway through a transaction.
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='banking_auth_grants'").fetchone()
    grant=db.execute("SELECT * FROM banking_auth_grants WHERE session_id=? AND action_kind=? AND action_id=?",(sid,kind,row["id"])).fetchone() if exists else None
    if not grant or grant["consumed"] or grant["expires_at"]<=_now().isoformat() or not secrets.compare_digest(grant["digest"],row["digest"]): raise HTTPException(403,"缺少绑定此操作的设备验证凭据")
    db.execute("UPDATE banking_auth_grants SET consumed=1 WHERE session_id=? AND action_kind=? AND action_id=?",(sid,kind,row["id"]))


@router.post('/device-check/options')
def device_check_options(x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=_session(db,x_banking_session)
        keys=db.execute('SELECT id FROM banking_passkeys WHERE session_id=?',(user['id'],)).fetchall()
        if not keys:raise HTTPException(409,'请先绑定通行密钥，再验证设备')
        options=generate_authentication_options(rp_id=config()[0],allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(key['id'])) for key in keys],user_verification=UserVerificationRequirement.REQUIRED,timeout=120000)
        return _challenge(db,user['id'],'device-check',options)


@router.post('/device-check/verify')
def device_check_verify(req: Verify,x_banking_session: str|None=Header(default=None)):
    with _connect() as db:
        schema(db);db.execute('BEGIN IMMEDIATE');user=_session(db,x_banking_session);challenge=_load_challenge(db,user,req,'device-check')
        key=db.execute('SELECT * FROM banking_passkeys WHERE id=? AND session_id=?',(req.credential.get('id'),user['id'])).fetchone()
        if not key:_failure(db,user,challenge)
        try:result=verify_authentication_response(credential=req.credential,expected_challenge=base64url_to_bytes(challenge['challenge']),expected_rp_id=config()[0],expected_origin=config()[1],credential_public_key=key['public_key'],credential_current_sign_count=key['sign_count'],require_user_verification=True)
        except Exception:_failure(db,user,challenge)
        db.execute('UPDATE banking_passkeys SET sign_count=? WHERE id=?',(result.new_sign_count,key['id']))
        db.execute('UPDATE banking_auth_challenges SET used=1 WHERE id=?',(challenge['id'],))
        report={'verification_id':secrets.token_hex(12),'checked_at':_now().isoformat(),'origin':config()[1],'rp_id':config()[0],'user_verified':True,'funds_moved':False,'scope':'device_signature_check'}
        _audit(db,user['id'],None,'DEVICE_CHECK_PASSED',report)
        return {'verified':True,**report}


@router.get('/device-check/status')
def device_check_status(x_banking_session: str|None=Header(default=None)):
    import json
    with _connect() as db:
        user=_session(db,x_banking_session)
        row=db.execute("SELECT detail FROM sandbox_audit WHERE session_id=? AND event='DEVICE_CHECK_PASSED' ORDER BY id DESC LIMIT 1",(user['id'],)).fetchone()
        return {'verified':bool(row),'result':json.loads(row['detail']) if row else None}
