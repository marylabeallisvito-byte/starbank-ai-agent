import unittest
from datetime import timedelta
from unittest.mock import patch
from fastapi import HTTPException
from backend.tests import test_banking_storage as fixtures
from backend import banking as b,banking_identity as identity,banking_missions as m


class IdentityTests(unittest.TestCase):
    setUp=fixtures.StorageTests.setUp
    tearDown=fixtures.StorageTests.tearDown

    def account(self,identifier,role):
        with b._connect() as db:
            identity.schema(db)
            return identity.create(db,identifier,'隔离测试身份',role)

    def test_viewer_cannot_decide_and_auditor_cannot_read_review_queue(self):
        viewer=self.account('viewer-1','viewer');auditor=self.account('auditor-1','auditor')
        self.assertEqual(identity.require(viewer,'review.read')['id'],'viewer-1')
        with self.assertRaises(HTTPException):identity.require(viewer,'review.decide')
        with self.assertRaises(HTTPException):m.inbox(auditor)
        self.assertEqual(identity.require(auditor,'audit.read')['role'],'auditor')

    def test_revocation_is_immediate_and_customer_token_never_grants_role(self):
        token=self.account('reviewer-1','reviewer')
        self.assertEqual(identity.require(token,'review.decide')['role'],'reviewer')
        with b._connect() as db:db.execute("UPDATE banking_operator_identities SET enabled=0 WHERE id='reviewer-1'")
        with self.assertRaises(HTTPException):identity.require(token,'review.read')
        with self.assertRaises(HTTPException):identity.require(self.token,'review.decide')

    def test_database_contains_token_hash_not_credential(self):
        token=self.account('readonly-2','viewer')
        with b._connect() as db:
            row=db.execute("SELECT * FROM banking_operator_identities WHERE id='readonly-2'").fetchone()
            self.assertNotEqual(row['token_hash'],token)
            self.assertEqual(len(row['token_hash']),64)
            self.assertEqual(row['display_name'],'隔离测试身份')

    def test_short_session_keeps_original_role_and_stores_only_hash(self):
        credential=self.account('session-viewer','viewer')
        signed=identity.start_workforce_session(credential,'isolated-client')
        token=signed['session_token']
        self.assertNotEqual(token,credential)
        self.assertEqual(identity.require(token,'review.read')['role'],'viewer')
        with self.assertRaises(HTTPException):identity.require(token,'review.decide')
        with b._connect() as db:
            row=db.execute('SELECT token_hash FROM banking_operator_sessions').fetchone()
            self.assertNotEqual(row[0],token)

    def test_expired_or_logged_out_session_cannot_read_reviews(self):
        credential=self.account('session-reviewer','reviewer')
        signed=identity.start_workforce_session(credential,'isolated-client')
        now=b._now()
        with patch.object(b,'_now',return_value=now+timedelta(minutes=16)):
            with self.assertRaises(HTTPException):identity.require(signed['session_token'],'review.read')
        self.assertTrue(identity.logout(signed['session_token'])['signed_out'])
        with self.assertRaises(HTTPException):identity.require(signed['session_token'],'review.read')
        self.assertEqual(identity.require(credential,'review.read')['id'],'session-reviewer')

    def test_revocation_and_role_change_apply_to_already_logged_in_session(self):
        credential=self.account('session-change','reviewer')
        token=identity.start_workforce_session(credential,'isolated-client')['session_token']
        with b._connect() as db:db.execute("UPDATE banking_operator_identities SET role='viewer' WHERE id='session-change'")
        with self.assertRaises(HTTPException):identity.require(token,'review.decide')
        self.assertEqual(identity.require(token,'review.read')['role'],'viewer')
        with b._connect() as db:db.execute("UPDATE banking_operator_identities SET enabled=0 WHERE id='session-change'")
        with self.assertRaises(HTTPException):identity.require(token,'review.read')

    def test_customer_unknown_and_auditor_credentials_cannot_login_to_review(self):
        auditor=self.account('session-auditor','auditor')
        for credential in (self.token,'unknown-valid-length-token-123456',auditor):
            with self.assertRaises(HTTPException):identity.start_workforce_session(credential,'isolated-client')

    def test_session_cannot_refresh_itself_and_bad_login_is_rate_limited(self):
        credential=self.account('session-no-renew','reviewer')
        token=identity.start_workforce_session(credential,'isolated-client')['session_token']
        with self.assertRaises(HTTPException):identity.start_workforce_session(token,'isolated-client')
        for _ in range(10):
            with self.assertRaises(HTTPException):identity.start_workforce_session('invalid','rate-client')
        with self.assertRaises(HTTPException) as error:identity.start_workforce_session(credential,'rate-client')
        self.assertEqual(error.exception.status_code,429)
        now=b._now()
        with patch.object(b,'_now',return_value=now+timedelta(minutes=2)):
            self.assertEqual(identity.start_workforce_session(credential,'rate-client')['identity']['id'],'session-no-renew')

    def test_http_workforce_login_identity_and_logout(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app=FastAPI();app.include_router(b.router)
        credential=self.account('http-reviewer','reviewer')
        client=TestClient(app)
        signed=client.post('/api/banking/operator/login',headers={'X-Banking-Operator':credential})
        self.assertEqual(signed.status_code,200);self.assertEqual(signed.headers['cache-control'],'no-store')
        headers={'X-Banking-Operator':signed.json()['session_token']}
        self.assertEqual(client.get('/api/banking/operator/identity',headers=headers).json()['id'],'http-reviewer')
        self.assertEqual(client.post('/api/banking/operator/logout',headers=headers).status_code,200)
        self.assertEqual(client.get('/api/banking/operator/identity',headers=headers).status_code,403)
