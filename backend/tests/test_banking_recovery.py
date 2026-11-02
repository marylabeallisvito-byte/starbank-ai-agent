import os
import base64
import unittest
from fastapi import HTTPException
from backend.tests import test_banking_trust as fixtures
from backend import banking as b,banking_missions as m,banking_mission_evidence as e,banking_replanning as r
from backend import banking_statements as statements
from backend.tests import test_banking_planning as planning


class RecoveryTests(unittest.TestCase):
    setUp=fixtures.TrustTests.setUp
    tearDown=fixtures.TrustTests.tearDown
    mission=fixtures.TrustTests.mission
    approve=fixtures.TrustTests.approve
    import_rows=planning.PlanningTests.import_rows

    def add_bill(self,mid,identifier='added'):
        with b._connect() as db:
            sid=db.execute('SELECT session_id FROM banking_missions WHERE id=?',(mid,)).fetchone()[0]
            db.execute('INSERT INTO sandbox_bills(id,session_id,merchant,category,amount_fen,occurred_at) VALUES(?,?,?,?,?,?)',(identifier,sid,'恢复测试','其他',100,b._now().isoformat()))

    def test_operator_read_requires_separate_credential_and_pending_review(self):
        plan=self.mission()
        for _ in range(3): m.tick(plan['id'])
        review=m.list_missions(self.token)['items'][0]['reviews'][0]
        with self.assertRaises(HTTPException): e.operator_evidence(review['id'],0,self.token)
        record=e.operator_evidence(review['id'],0,os.environ['BANKING_OPERATOR_TOKEN'])
        self.assertEqual(record['status'],'available')
        with self.assertRaises(HTTPException): m.decide(review['id'],m.ReviewDecision(decision='approve',note='已完成来源检查'),os.environ['BANKING_OPERATOR_TOKEN'])
        m.decide(review['id'],m.ReviewDecision(decision='approve',note='已完成来源检查',evidence_digest=record['snapshot_hash']),os.environ['BANKING_OPERATOR_TOKEN'])
        with self.assertRaises(HTTPException): e.operator_evidence(review['id'],0,os.environ['BANKING_OPERATOR_TOKEN'])
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_operator_cannot_approve_changed_source_with_old_hash(self):
        plan=self.mission()
        for _ in range(3): m.tick(plan['id'])
        review=m.list_missions(self.token)['items'][0]['reviews'][0]
        record=e.operator_evidence(review['id'],0,os.environ['BANKING_OPERATOR_TOKEN'])
        self.add_bill(plan['id'])
        with self.assertRaises(HTTPException): m.decide(review['id'],m.ReviewDecision(decision='approve',note='已核查旧快照',evidence_digest=record['snapshot_hash']),os.environ['BANKING_OPERATOR_TOKEN'])

    def test_replanning_preserves_budget_and_cancellation_without_reexecution(self):
        with b._connect() as db: db.execute('DELETE FROM sandbox_bills')
        plan=self.mission(cancel=True)
        for _ in range(5): m.tick(plan['id'])
        m.action(plan['id'],m.MissionAction(action='pause'),self.token)
        self.add_bill(plan['id'])
        preview=r.changes(plan['id'],self.token)
        self.assertEqual(set(preview['preserved_nodes']),{'reserve','cancel_subscription'})
        updated=r.accept(plan['id'],r.AcceptChanges(proposal_digest=preview['proposal_digest']),self.token)
        self.assertEqual(updated['status'],'PAUSED')
        self.assertNotEqual(updated['digest'],plan['digest'])
        self.assertEqual(b.overview(self.token)['reserved'],'500.00')
        m.action(plan['id'],m.MissionAction(action='resume'),self.token)
        for _ in range(4): m.tick(plan['id'])
        result=m.list_missions(self.token)['items'][0]
        self.assertEqual(result['status'],'COMPLETED')
        self.assertEqual(b.overview(self.token)['reserved'],'500.00')
        self.assertEqual(sum(ev['event']=='NODE_CHECKPOINT' and '"node_id": "reserve"' in ev['detail'] for ev in result['events']),1)

    def test_stale_change_preview_and_cross_session_are_rejected(self):
        plan=self.mission();m.tick(plan['id']);m.action(plan['id'],m.MissionAction(action='pause'),self.token)
        self.add_bill(plan['id'])
        preview=r.changes(plan['id'],self.token)
        self.add_bill(plan['id'],'second')
        with self.assertRaises(HTTPException): r.accept(plan['id'],r.AcceptChanges(proposal_digest=preview['proposal_digest']),self.token)
        with self.assertRaises(HTTPException): r.changes(plan['id'],b.create_session()['token'])
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_rejected_review_still_requires_independent_review_after_change(self):
        plan=self.mission()
        for _ in range(3):m.tick(plan['id'])
        review=m.list_missions(self.token)['items'][0]['reviews'][0]
        m.decide(review['id'],m.ReviewDecision(decision='reject',note='资料不足不予通过'),os.environ['BANKING_OPERATOR_TOKEN'])
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        preview=r.changes(plan['id'],self.token)
        r.accept(plan['id'],r.AcceptChanges(proposal_digest=preview['proposal_digest']),self.token)
        m.action(plan['id'],m.MissionAction(action='resume'),self.token)
        for _ in range(3):m.tick(plan['id'])
        self.assertEqual(m.list_missions(self.token)['items'][0]['status'],'WAITING_REVIEW')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_unrelated_currency_import_does_not_invalidate_cny_task(self):
        self.import_rows()
        plan=m.prepare(m.MissionRequest(amount='500',bill_source='imported'),self.token)
        csv=f"日期,商户,金额,币种,交易单号\n{b._now().strftime('%Y-%m-%d')},其他来源,19,HKD,unrelated-hkd\n"
        preview=statements.preview(statements.PreviewRequest(filename='unrelated.csv',content_base64=base64.b64encode(csv.encode()).decode(),confirmed_local_parse=True,sign_convention='positive_expense'),self.token)
        statements.confirm(preview['id'],statements.Confirm(digest=preview['digest'],confirmed=True),self.token)
        with b._connect() as db:
            sid=b._session(db,self.token)['id']
            self.assertEqual(m.imported_snapshot(db,sid)[0],plan['input']['source_manifest'])
        m.action(plan['id'],m.MissionAction(action='confirm',digest=plan['digest']),self.token)

    def test_removing_pending_anomaly_does_not_skip_independent_review(self):
        plan=self.mission()
        for _ in range(3):m.tick(plan['id'])
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        preview=r.changes(plan['id'],self.token)
        r.accept(plan['id'],r.AcceptChanges(proposal_digest=preview['proposal_digest']),self.token)
        m.action(plan['id'],m.MissionAction(action='resume'),self.token)
        for _ in range(3):m.tick(plan['id'])
        self.assertEqual(m.list_missions(self.token)['items'][0]['status'],'WAITING_REVIEW')
