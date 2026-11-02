"""Monthly outcome semantics, explicit exception scope and atomic confirmation."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi import HTTPException
from backend import banking as b,banking_goal_planner as g,banking_missions as m,banking_replanning as replanning
from backend.tests import test_banking_evidence as fixtures

OPERATOR='monthly-reviewer-test-token-12345678'


class MonthlyTests(unittest.TestCase):
    def setUp(self):
        self.key=patch.dict('os.environ',{'BANKING_DATA_ENCRYPTION_KEY':Fernet.generate_key().decode(),'BANKING_DATA_PREVIOUS_KEYS':'','BANKING_OPERATOR_TOKEN':OPERATOR});self.key.start()
        fixtures.BankingEvidenceTests.setUp(self)
    def tearDown(self):fixtures.BankingEvidenceTests.tearDown(self);self.key.stop()
    def goal(self):return g.prepare(g.GoalRequest(text='核对本月账单，预留500元，至少保留1000元生活费，取消云上视频订阅，如果发现重复扣费先暂停'),self.token)
    def opted(self):
        original=self.goal()
        return g.edit(original['id'],g.GoalEdit(digest=original['digest'],risk_policy='reserve_before_review',acknowledge_policy_change=True),self.token)
    def activate(self,proposal):return g.activate(proposal['id'],g.Activate(digest=proposal['digest'],confirmed=True),self.token)
    def current(self,mission):return m.get_mission(mission['id'],self.token)
    def ticks(self,mission,count):
        for _ in range(count):m.tick(mission['id'])
        return self.current(mission)
    def decision(self,mission,decision):
        current=self.current(mission);review=current['reviews'][-1]
        with b._connect() as db:proof=db.execute('SELECT content_hash FROM banking_mission_sources WHERE mission_id=?',(mission['id'],)).fetchone()[0]
        return m.decide(review['id'],m.ReviewDecision(decision=decision,note='已核对原始记录与当前资金安排',evidence_digest=proof),OPERATOR)

    def test_projection_is_ledger_based_and_does_not_count_savings_or_refunds(self):
        proposal=self.goal();monthly=proposal['monthly'];f=monthly['funding']
        self.assertEqual((f['cash_total_fen'],f['target_budget_fen'],f['minimum_balance_fen'],f['headroom_fen']),(250000,50000,100000,100000))
        self.assertTrue(f['projection']);self.assertFalse(monthly['subscription']['savings_verified']);self.assertFalse(monthly['subscription']['refund_included'])
        self.assertEqual(monthly['exception']['finding_count'],1)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_missing_floor_is_not_reported_as_zero_or_free_cash(self):
        proposal=g.prepare(g.GoalRequest(text='核对账单，预留500元，别影响房租'),self.token)
        f=proposal['monthly']['funding'];self.assertFalse(f['complete']);self.assertFalse(f['floor_known']);self.assertIsNone(f['headroom_fen'])

    def test_not_yet_analyzed_mission_is_not_claimed_to_have_zero_findings(self):
        mission=self.activate(self.goal());report=mission['monthly']['exception']
        self.assertFalse(report['analysis_checked']);self.assertIsNone(report['finding_count'])
        analyzed=self.ticks(mission,1)['monthly']['exception']
        self.assertTrue(analyzed['analysis_checked']);self.assertEqual(analyzed['finding_count'],1)

    def test_default_policy_waits_for_review_before_any_reservation(self):
        mission=self.activate(self.goal());current=self.ticks(mission,3)
        self.assertEqual(current['status'],'WAITING_REVIEW');self.assertEqual(b.overview(self.token)['reserved'],'0.00')
        self.assertEqual(current['monthly']['effects'][0]['state'],'planned')

    def test_workbench_can_target_one_pending_mission_without_mixing_reviews(self):
        first=self.activate(self.goal());second=self.activate(self.goal())
        self.ticks(first,3);self.ticks(second,3)
        all_rows=m.inbox(OPERATOR)['items']
        self.assertEqual({row['mission']['id'] for row in all_rows},{first['id'],second['id']})
        targeted=m.inbox(OPERATOR,first['id'])['items']
        self.assertEqual(len(targeted),1)
        self.assertEqual(targeted[0]['mission']['id'],first['id'])
        self.assertEqual(m.inbox(OPERATOR,'missing-task')['items'],[])

    def test_targeted_review_queue_still_requires_independent_identity(self):
        mission=self.activate(self.goal());self.ticks(mission,3)
        with self.assertRaises(HTTPException) as error:m.inbox(self.token,mission['id'])
        self.assertIn(error.exception.status_code,(401,403))
        self.assertEqual(self.current(mission)['status'],'WAITING_REVIEW')

    def test_review_handoff_exposes_saved_times_and_note_then_leaves_customer_in_control(self):
        mission=self.activate(self.goal());pending=self.ticks(mission,3)
        review=pending['reviews'][-1]
        self.assertTrue(review['created_at']);self.assertIsNone(review['decided_at'])
        approved=self.decision(mission,'approve');saved=approved['reviews'][-1]
        self.assertEqual(saved['created_at'],review['created_at'])
        self.assertTrue(saved['decided_at']);self.assertEqual(saved['decision_note'],'已核对原始记录与当前资金安排')
        self.assertEqual(m.inbox(OPERATOR,mission['id'])['items'],[])
        self.ticks(mission,4)
        self.assertEqual(self.current(mission)['status'],'PAUSED')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_exception_scope_change_requires_explicit_acknowledgment_and_new_digest(self):
        original=self.goal()
        with self.assertRaises(HTTPException):g.edit(original['id'],g.GoalEdit(digest=original['digest'],risk_policy='reserve_before_review'),self.token)
        revised=g.edit(original['id'],g.GoalEdit(digest=original['digest'],risk_policy='reserve_before_review',acknowledge_policy_change=True),self.token)
        self.assertNotEqual(original['digest'],revised['digest'])
        with self.assertRaises(HTTPException):self.activate(original)
        self.assertTrue(revised['monthly']['exception']['budget_may_be_reserved_before_review'])

    def test_opted_budget_can_be_held_but_subscription_still_waits_for_review(self):
        mission=self.activate(self.opted());current=self.ticks(mission,4)
        self.assertEqual(current['status'],'WAITING_REVIEW');self.assertEqual(b.overview(self.token)['reserved'],'500.00')
        self.assertEqual(current['monthly']['funding']['headroom_fen'],100000)
        self.assertEqual(current['monthly']['subscription']['current_status'],'ACTIVE')
        self.assertEqual(current['monthly']['effects'][1]['state'],'waiting_review')
        self.ticks(mission,5);self.assertEqual(b.overview(self.token)['reserved'],'500.00')

    def test_rejected_review_keeps_cancellation_stopped_and_budget_can_be_released(self):
        mission=self.activate(self.opted());self.ticks(mission,4);current=self.decision(mission,'reject')
        with self.assertRaises(HTTPException):m.action(mission['id'],m.MissionAction(action='resume'),self.token)
        self.assertEqual(current['monthly']['subscription']['current_status'],'ACTIVE')
        stopped=m.action(mission['id'],m.MissionAction(action='cancel'),self.token)
        self.assertEqual(stopped['monthly']['funding']['budget_state'],'released');self.assertEqual(b.overview(self.token)['reserved'],'0.00')
        self.assertEqual(stopped['monthly']['effects'][1]['state'],'stopped')

    def test_review_approval_does_not_auto_resume_or_cancel_subscription(self):
        mission=self.activate(self.opted());self.ticks(mission,4);approved=self.decision(mission,'approve')
        self.assertEqual(approved['status'],'PAUSED');self.assertEqual(approved['monthly']['subscription']['current_status'],'ACTIVE')
        m.action(mission['id'],m.MissionAction(action='resume'),self.token);done=self.ticks(mission,3)
        self.assertEqual(done['status'],'COMPLETED');self.assertEqual(done['monthly']['effects'][1]['state'],'cancelled')
        self.assertFalse(done['monthly']['subscription']['savings_verified']);self.assertEqual(b.overview(self.token)['reserved'],'500.00')

    def test_source_change_still_blocks_opted_budget_before_effect(self):
        mission=self.activate(self.opted());self.ticks(mission,2)
        with b._connect() as db:db.execute('UPDATE sandbox_bills SET amount_fen=amount_fen+1')
        current=self.ticks(mission,1)
        self.assertEqual(current['status'],'BLOCKED');self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_opted_scope_never_accepts_other_risk_types_or_missing_acknowledgment(self):
        with b._connect() as db:
            m.schema(db);user=b._session(db,self.token)
            with self.assertRaises(HTTPException):m.prepare_in_db(db,user,m.MissionRequest(amount='100'),{'risk_policy':'reserve_before_review'})
        mission=self.activate(self.opted());self.ticks(mission,2)
        with b._connect() as db:db.execute('UPDATE banking_missions SET evidence_json=? WHERE id=?',(json.dumps([{'kind':'outside_scope_fraud_signal'}]),mission['id']))
        current=self.ticks(mission,1);self.assertEqual(current['status'],'BLOCKED');self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_account_lock_still_blocks_opted_budget(self):
        mission=self.activate(self.opted());self.ticks(mission,2)
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET locked_until=?',((b._now()+timedelta(minutes=10)).isoformat(),))
        current=self.ticks(mission,1);self.assertEqual(current['status'],'BLOCKED');self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_new_source_after_handoff_requires_review_before_opted_reservation(self):
        mission=self.activate(self.opted());self.ticks(mission,2)
        m.action(mission['id'],m.MissionAction(action='handoff'),self.token)
        with b._connect() as db:db.execute('UPDATE sandbox_bills SET amount_fen=amount_fen+1')
        changes=replanning.changes(mission['id'],self.token)
        replanning.accept(mission['id'],replanning.AcceptChanges(proposal_digest=changes['proposal_digest']),self.token)
        m.action(mission['id'],m.MissionAction(action='resume'),self.token);current=self.ticks(mission,2)
        self.assertEqual(current['status'],'WAITING_REVIEW');self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_confirmation_is_atomic_and_idempotent_under_concurrency(self):
        proposal=self.goal()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.activate(proposal),range(2)))
        self.assertEqual(results[0]['id'],results[1]['id'])
        with b._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_missions').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sandbox_audit WHERE event='MISSION_CONFIRM'").fetchone()[0],1)

    def test_no_confirmation_or_locked_account_cannot_leave_activated_or_orphan_plan(self):
        proposal=self.goal()
        with self.assertRaises(HTTPException):g.activate(proposal['id'],g.Activate(digest=proposal['digest'],confirmed=False),self.token)
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET locked_until=?',((b._now()+timedelta(minutes=10)).isoformat(),))
        with self.assertRaises(HTTPException):self.activate(proposal)
        with b._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_missions').fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT mission_id FROM banking_goal_proposals').fetchone()[0])

    def test_modified_compiled_plan_cannot_be_activated_using_old_goal(self):
        proposal=self.goal();mission=g.compile_goal(proposal['id'],g.Compile(digest=proposal['digest']),self.token)
        revised=m.revise(mission['id'],m.MissionRevision(amount='600',minimum_balance='1000',digest=mission['digest']),self.token)
        with self.assertRaises(HTTPException):self.activate(proposal)
        self.assertEqual(self.current(mission)['status'],'DRAFT')
        m.action(mission['id'],m.MissionAction(action='confirm',digest=revised['digest']),self.token)

    def test_repeated_confirmation_does_not_resume_a_paused_plan(self):
        proposal=self.goal();mission=self.activate(proposal)
        m.action(mission['id'],m.MissionAction(action='pause'),self.token)
        repeated=self.activate(proposal);self.assertEqual(repeated['status'],'PAUSED')

    def test_lost_activation_response_recovers_same_plan_and_single_confirmation(self):
        from backend.main import app
        from backend.tests.test_banking_journeys import DropOnce
        from starlette.testclient import TestClient
        proposal=self.goal();wrapper=DropOnce(app);client=TestClient(wrapper)
        path='/api/banking/goal-plans/'+proposal['id']+'/activate';wrapper.path=path
        body={'digest':proposal['digest'],'confirmed':True};headers={'X-Banking-Session':self.token}
        try:
            with self.assertRaisesRegex(RuntimeError,'injected response loss'):client.post(path,json=body,headers=headers)
            result=client.post(path,json=body,headers=headers);self.assertEqual(result.status_code,200)
            linked=g.read(proposal['id'],self.token);self.assertEqual(result.json()['id'],linked['mission_id'])
            with b._connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM sandbox_audit WHERE event='MISSION_CONFIRM'").fetchone()[0],1)
        finally:client.close()

    def test_other_existing_reservations_are_not_hidden_or_counted_twice(self):
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        first=m.prepare(m.MissionRequest(amount='200',minimum_balance='0'),self.token)
        m.action(first['id'],m.MissionAction(action='confirm',digest=first['digest']),self.token);self.ticks(first,6)
        proposal=self.opted();self.assertEqual(proposal['monthly']['funding']['headroom_fen'],80000)
        mission=self.activate(proposal);done=self.ticks(mission,8)
        f=done['monthly']['funding'];self.assertEqual(f['headroom_fen'],80000);self.assertEqual(f['existing_other_reserved_fen'],20000)

    def test_historical_goal_shows_linked_current_outcome_instead_of_unexecuted_projection(self):
        proposal=self.opted();mission=self.activate(proposal);self.ticks(mission,4)
        historical=g.read(proposal['id'],self.token)
        self.assertEqual(historical['monthly']['funding']['budget_state'],'held');self.assertFalse(historical['monthly']['funding']['projection'])
        m.action(mission['id'],m.MissionAction(action='cancel'),self.token)
        historical=g.read(proposal['id'],self.token)
        self.assertEqual(historical['monthly']['funding']['budget_state'],'released');self.assertEqual(historical['monthly']['funding']['plan_budget_fen'],0)
