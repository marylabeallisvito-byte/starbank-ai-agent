from __future__ import annotations
import base64
import hashlib
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from fastapi import HTTPException
from backend import banking as b, banking_auth as a, banking_missions as m

def db_hash(mid):
    with b._connect() as db: return db.execute('SELECT content_hash FROM banking_mission_sources WHERE mission_id=?',(mid,)).fetchone()[0]


def enc(data): return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"data")
        self.db=patch.object(b,"DB_PATH",Path(self.temp.name)/"trust.sqlite3"); self.db.start()
        self.env=patch.dict(os.environ,{"BANKING_AUTH_MODE":"passkey","BANKING_LLM_ENABLED":"0","BANKING_OPERATOR_TOKEN":"operator-test-token-123456789012345"}); self.env.start()
        self.token=b.create_session()["token"]
        b.confirm_subscription_query(b.QueryConsent(confirmed=True),self.token)

    def tearDown(self): self.env.stop(); self.db.stop(); self.temp.cleanup()

    def register(self):
        options=a.registration_options(self.token)
        self.key=ec.generate_private_key(ec.SECP256R1()); nums=self.key.public_key().public_numbers(); self.cid=secrets.token_bytes(32)
        cose={1:2,3:-7,-1:1,-2:nums.x.to_bytes(32,"big"),-3:nums.y.to_bytes(32,"big")}
        data=hashlib.sha256(b"localhost").digest()+bytes([0x45])+bytes(4)+bytes(16)+len(self.cid).to_bytes(2,"big")+self.cid+cbor2.dumps(cose)
        client=json.dumps({"type":"webauthn.create","challenge":options["options"]["challenge"],"origin":"http://localhost:5173","crossOrigin":False}).encode()
        credential={"id":enc(self.cid),"rawId":enc(self.cid),"type":"public-key","response":{"clientDataJSON":enc(client),"attestationObject":enc(cbor2.dumps({"fmt":"none","attStmt":{},"authData":data}))},"clientExtensionResults":{}}
        a.registration_verify(a.Verify(challenge_id=options["challenge_id"],credential=credential),self.token)

    def assertion(self,challenge,origin="http://localhost:5173",count=1,flags=0x05):
        client=json.dumps({"type":"webauthn.get","challenge":challenge,"origin":origin,"crossOrigin":False}).encode()
        data=hashlib.sha256(b"localhost").digest()+bytes([flags])+count.to_bytes(4,"big")
        signature=self.key.sign(data+hashlib.sha256(client).digest(),ec.ECDSA(hashes.SHA256()))
        return {"id":enc(self.cid),"rawId":enc(self.cid),"type":"public-key","response":{"clientDataJSON":enc(client),"authenticatorData":enc(data),"signature":enc(signature),"userHandle":None},"clientExtensionResults":{}}

    def red(self):
        t=b.prepare_transfer(b.PrepareTransfer(recipient_id="mom",amount="1001"),self.token)
        b.confirm_transfer(t["id"],b.ConfirmTransfer(digest=t["digest"]),self.token)
        return t

    def test_passkey_signature_authorizes_exact_red_transaction_and_cannot_replay(self):
        self.register(); t=self.red()
        with self.assertRaises(HTTPException): b.demo_code(t["id"],self.token)
        opts=a.authorization_options(a.Action(kind="transfer",action_id=t["id"]),self.token)
        req=a.Verify(challenge_id=opts["challenge_id"],credential=self.assertion(opts["options"]["challenge"]))
        a.authorization_verify(req,self.token)
        receipt=b.execute_transfer(t["id"],b.ExecuteTransfer(idempotency_key="passkey-transaction"),self.token)["receipt"]
        self.assertTrue(receipt.startswith("SBX-"))
        with self.assertRaises(HTTPException): a.authorization_verify(req,self.token)
        self.assertEqual(b.overview(self.token)["balance"],"1499.00")

    def test_passkey_wrong_origin_is_rejected_without_funds_moving(self):
        self.register(); t=self.red(); opts=a.authorization_options(a.Action(kind="transfer",action_id=t["id"]),self.token)
        with self.assertRaises(HTTPException): a.authorization_verify(a.Verify(challenge_id=opts["challenge_id"],credential=self.assertion(opts["options"]["challenge"],"https://attacker.test")),self.token)
        self.assertEqual(b.overview(self.token)["balance"],"2500.00")

    def test_forged_ready_status_without_passkey_grant_cannot_execute(self):
        t=self.red()
        with b._connect() as db: db.execute("UPDATE sandbox_transfers SET status='READY' WHERE id=?",(t["id"],))
        with self.assertRaises(HTTPException): b.execute_transfer(t["id"],b.ExecuteTransfer(idempotency_key="forged-transaction"),self.token)

    def test_valid_signature_without_device_user_verification_is_rejected(self):
        self.register(); t=self.red(); opts=a.authorization_options(a.Action(kind="transfer",action_id=t["id"]),self.token)
        with self.assertRaises(HTTPException): a.authorization_verify(a.Verify(challenge_id=opts["challenge_id"],credential=self.assertion(opts["options"]["challenge"],flags=0x01)),self.token)
        self.assertEqual(b.overview(self.token)["balance"],"2500.00")

    def test_passkey_challenge_cannot_cross_session_or_changed_digest(self):
        self.register(); t=self.red(); opts=a.authorization_options(a.Action(kind="transfer",action_id=t["id"]),self.token)
        req=a.Verify(challenge_id=opts["challenge_id"],credential=self.assertion(opts["options"]["challenge"]))
        with self.assertRaises(HTTPException): a.authorization_verify(req,b.create_session()["token"])
        with b._connect() as db: db.execute("UPDATE sandbox_transfers SET digest=? WHERE id=?",("f"*64,t["id"]))
        with self.assertRaises(HTTPException): a.authorization_verify(req,self.token)
        self.assertEqual(b.overview(self.token)["balance"],"2500.00")

    def mission(self,amount="500",floor="1000",cancel=True):
        sub=b.subscriptions(self.token)["items"][0]
        plan=m.prepare(m.MissionRequest(amount=amount,minimum_balance=floor,subscription_id=sub["id"],cancel_subscription=cancel),self.token)
        m.action(plan["id"],m.MissionAction(action="confirm",digest=plan["digest"]),self.token)
        return plan

    def approve(self,plan):
        for _ in range(3): m.tick(plan["id"])
        row=m.list_missions(self.token)["items"][0]
        self.assertEqual(row["status"],"WAITING_REVIEW")
        with self.assertRaises(HTTPException): m.action(plan["id"],m.MissionAction(action="resume"),self.token)
        m.decide(row["reviews"][0]["id"],m.ReviewDecision(decision="approve",note="已核对仿真交易，允许继续处理",evidence_digest=db_hash(plan["id"])),os.environ["BANKING_OPERATOR_TOKEN"])
        m.action(plan["id"],m.MissionAction(action="resume"),self.token)

    def test_mission_handoff_checkpoints_and_compensation(self):
        plan=self.mission(); self.approve(plan)
        m.tick(plan["id"])
        self.assertEqual(b.overview(self.token)["reserved"],"500.00")
        m.action(plan["id"],m.MissionAction(action="pause"),self.token)
        self.assertFalse(m.tick(plan["id"]))
        m.action(plan["id"],m.MissionAction(action="resume"),self.token)
        m.tick(plan["id"]); m.tick(plan["id"])
        self.assertEqual(m.list_missions(self.token)["items"][0]["status"],"COMPLETED")
        m.action(plan["id"],m.MissionAction(action="release"),self.token)
        self.assertEqual(b.overview(self.token)["balance"],"2500.00")
        self.assertEqual(next(x for x in b.subscriptions(self.token)["items"] if x["id"]==plan["input"]["subscription_id"])["status"],"CANCELLED")

    def test_concurrent_checkpoint_reserves_once_and_cancel_releases_once(self):
        plan=self.mission(cancel=False); self.approve(plan)
        with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(lambda _:m.tick(plan["id"]),range(2)))
        self.assertEqual(b.overview(self.token)["reserved"],"500.00")
        row=m.list_missions(self.token)["items"][0]
        m.action(plan["id"],m.MissionAction(action="release" if row["status"]=="COMPLETED" else "cancel"),self.token)
        self.assertEqual(b.overview(self.token)["reserved"],"0.00")

    def test_insufficient_floor_blocks_without_changing_amount(self):
        plan=self.mission(amount="2000",floor="1000",cancel=False)
        for _ in range(3): m.tick(plan["id"])
        row=m.list_missions(self.token)["items"][0]
        self.assertEqual(row["status"],"BLOCKED"); self.assertEqual(row["input"]["amount"],"2000.00")
        self.assertEqual(b.overview(self.token)["reserved"],"0.00")

    def test_operator_requires_separate_credential(self):
        with self.assertRaises(HTTPException): m.inbox(self.token)

    def test_rejected_review_cannot_be_resumed_by_customer(self):
        plan=self.mission()
        for _ in range(3): m.tick(plan["id"])
        review=m.list_missions(self.token)["items"][0]["reviews"][0]
        m.decide(review["id"],m.ReviewDecision(decision="reject",note="证据尚未核对完成，拒绝继续"),os.environ["BANKING_OPERATOR_TOKEN"])
        with self.assertRaises(HTTPException): m.action(plan["id"],m.MissionAction(action="resume"),self.token)
        self.assertEqual(b.overview(self.token)["reserved"],"0.00")

    def test_partial_stop_compensates_budget_but_does_not_undo_completed_cancellation(self):
        plan=self.mission(); self.approve(plan); m.tick(plan["id"]); m.tick(plan["id"])
        result=m.action(plan["id"],m.MissionAction(action="cancel"),self.token)
        self.assertEqual(result["status"],"PARTIAL_STOPPED")
        self.assertEqual(b.overview(self.token)["reserved"],"0.00")
        self.assertEqual(next(x for x in b.subscriptions(self.token)["items"] if x["id"]==plan["input"]["subscription_id"])["status"],"CANCELLED")

    def test_subscription_query_requires_explicit_yellow_consent(self):
        token=b.create_session()["token"]
        with self.assertRaises(HTTPException): b.subscriptions(token)
        with self.assertRaises(HTTPException): b.confirm_subscription_query(b.QueryConsent(confirmed=False),token)
        b.confirm_subscription_query(b.QueryConsent(confirmed=True),token)
        self.assertEqual(len(b.subscriptions(token)["items"]),3)

    def test_compound_request_never_drops_unhandled_amount_or_timing(self):
        for i,text in enumerate(("检查账单，预留500元或者600元", "明天检查账单并预留500元", "检查账单，给妈妈转800元并预留500元")):
            result=m.message(m.MessageRequest(text=text,client_id=f"ambiguous-message-{i}"),self.token)
            if result['attachment']:
                from backend import banking_goal_planner as goals
                self.assertEqual(result['attachment']['kind'],'goal_plan')
                proposal=goals.read(result['attachment']['goal_id'],self.token)
                self.assertIn(proposal['status'],{'NEEDS_INPUT','BLOCKED'})
            else:self.assertIsNone(result['attachment'])
        self.assertEqual(len(m.list_missions(self.token)["items"]),0)

    def test_conversation_is_persistent_and_replay_does_not_duplicate_draft(self):
        req=m.MessageRequest(text="检查重复扣费，取消云上视频订阅，预留500元预算，保留1000元生活费",client_id="message-unique-123")
        first=m.message(req,self.token); second=m.message(req,self.token)
        self.assertEqual(first,second)
        self.assertEqual(first['attachment']['kind'],'goal_plan')
        self.assertEqual(len(m.list_missions(self.token)["items"]),0)
        with b._connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_goal_proposals').fetchone()[0],1)
        self.assertEqual(len(m.conversation(self.token)["items"]),2)
