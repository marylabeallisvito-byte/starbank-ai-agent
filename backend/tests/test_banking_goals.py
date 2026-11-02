"""Bounded goal compilation, literal provenance, authorization and scheduling checks."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi import HTTPException
from backend import banking as b,banking_missions as m,banking_goal_planner as g
from backend.tests import test_banking_evidence as fixtures


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.key=patch.dict('os.environ',{'BANKING_DATA_ENCRYPTION_KEY':Fernet.generate_key().decode(),'BANKING_DATA_PREVIOUS_KEYS':''});self.key.start()
        fixtures.BankingEvidenceTests.setUp(self)
    def tearDown(self):
        fixtures.BankingEvidenceTests.tearDown(self);self.key.stop()
    def propose(self,text):return g.prepare(g.GoalRequest(text=text),self.token)
    def compile(self,proposal):return g.compile_goal(proposal['id'],g.Compile(digest=proposal['digest']),self.token)
    def edit(self,proposal,**values):return g.edit(proposal['id'],g.GoalEdit(digest=proposal['digest'],**values),self.token)
    def ready(self):return self.propose('明天上午10点核对账单，预留500元，至少保留1000元生活费，如果发现重复扣费先暂停交人工核对')

    def test_literal_constraints_time_and_sources_survive_compilation(self):
        proposal=self.ready();self.assertEqual(proposal['status'],'READY')
        self.assertEqual(proposal['plan']['amount'],'500.00');self.assertEqual(proposal['plan']['minimum_balance'],'1000.00')
        self.assertIn('保留1000元',proposal['plan']['provenance']['minimum_balance']['quote'])
        mission=self.compile(proposal)
        self.assertEqual(mission['status'],'DRAFT');self.assertEqual(mission['input']['not_before'],proposal['plan']['not_before'])
        self.assertEqual(mission['input']['risk_policy'],'pause_all_on_suspicion');self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_missing_rent_floor_must_be_explicitly_completed(self):
        original=self.propose('核对账单，为旅行预留500元，别影响房租')
        self.assertEqual(original['status'],'NEEDS_INPUT');self.assertEqual(original['questions'][0]['field'],'minimum_balance')
        with self.assertRaises(HTTPException):self.compile(original)
        revised=self.edit(original,minimum_balance='1000')
        self.assertEqual(revised['status'],'READY');self.assertNotEqual(original['digest'],revised['digest'])
        self.assertEqual(revised['plan']['provenance']['minimum_balance']['source'],'user_edit')
        with self.assertRaises(HTTPException):self.compile(original)
        self.compile(revised)

    def test_missing_budget_and_tomorrow_without_time_are_not_guessed(self):
        proposal=self.propose('明天核对账单，为旅行预留预算，别影响房租')
        self.assertEqual({q['field'] for q in proposal['questions']},{'amount','minimum_balance','not_before'})
        revised=self.edit(proposal,amount='300',minimum_balance='1200',execution_mode='scheduled',not_before=b._now()+timedelta(days=1))
        self.assertEqual(revised['status'],'READY')

    def test_negated_subscription_cancellation_is_not_added_to_plan(self):
        for word in ('不要','不用','不需要','无需','别'):
            with self.subTest(word=word):
                proposal=self.propose(word+'取消云上视频订阅，核对账单，预留500元，至少保留1000元生活费')
                self.assertFalse(proposal['plan']['cancel_subscription'])
                mission=self.compile(proposal)
                self.assertNotIn('cancel_subscription',{n['id'] for n in mission['nodes']})

    def test_negated_budget_cannot_become_an_executable_plan_after_edit(self):
        for word in ('不要','不需要','不用','无需','别自动'):
            text='检查账单，'+word+'预留500元'
            self.assertTrue(g.applies(text))
            proposal=self.propose(text);self.assertEqual(proposal['status'],'BLOCKED')
            changed=self.edit(proposal,minimum_balance='1000')
            with self.assertRaises(HTTPException):self.compile(changed)

    def test_unsupported_goals_and_fallbacks_cannot_be_dropped_by_editing_amount(self):
        for extra in ('如果余额不足就赎回基金补足','并退款20元','每月自动重复','如果妈妈需要就转账800元','如果没有异常就买鲜花','预算改用usd'):
            with self.subTest(extra=extra):
                proposal=self.propose('核对账单，预留500元，至少保留1000元，'+extra)
                self.assertEqual(proposal['status'],'BLOCKED');self.assertTrue(proposal['plan']['blockers'])
                changed=self.edit(proposal,amount='400')
                with self.assertRaises(HTTPException):self.compile(changed)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_deadline_range_and_ambiguous_amounts_block_compilation(self):
        for text in ('明天10点之前核对账单，预留500元','明天10点到12点核对账单，预留500元','核对账单，预留500元或者800元','核对账单，预留500元，生活费保留100元或200元'):
            with self.subTest(text=text):
                proposal=self.propose(text);self.assertEqual(proposal['status'],'BLOCKED')
                with self.assertRaises(HTTPException):self.compile(proposal)

    def test_time_aliases_do_not_default_to_immediate_execution(self):
        for expression in ('周末','今晚','下班后','3天后'):
            text=expression+'核对账单，预留500元，至少保留1000元'
            self.assertTrue(g.applies(text))
            proposal=self.propose(text)
            self.assertIn('not_before',{q['field'] for q in proposal['questions']})
        half=self.propose('明天下午3点半核对账单，预留500元')
        self.assertIn('15:30:00',half['plan']['not_before'])

    def test_mission_cannot_run_before_authorized_time(self):
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        proposal=self.ready();mission=self.compile(proposal)
        m.action(mission['id'],m.MissionAction(action='confirm',digest=mission['digest']),self.token)
        self.assertFalse(m.tick(mission['id']));self.assertEqual(b.overview(self.token)['reserved'],'0.00')
        start=b._now()+timedelta(days=2)
        with patch.object(b,'_now',return_value=start),patch.object(m,'_now',return_value=start):
            for _ in range(6):m.tick(mission['id'])
            self.assertEqual(b.overview(self.token)['reserved'],'500.00')
        self.assertEqual(m.get_mission(mission['id'],self.token)['status'],'COMPLETED')

    def test_time_tampering_is_detected_before_waiting_for_execution(self):
        mission=self.compile(self.ready())
        m.action(mission['id'],m.MissionAction(action='confirm',digest=mission['digest']),self.token)
        with b._connect() as db:
            inputs=mission['input'];inputs['not_before']=(b._now()+timedelta(days=4)).isoformat()
            db.execute('UPDATE banking_missions SET input_json=? WHERE id=?',(json.dumps(inputs),mission['id']))
        m.tick(mission['id'])
        self.assertEqual(m.get_mission(mission['id'],self.token)['status'],'BLOCKED')

    def test_many_future_tasks_do_not_starve_a_ready_task(self):
        with b._connect() as db:
            m.schema(db);sid=db.execute('SELECT id FROM sandbox_sessions').fetchone()[0]
            later=(b._now()+timedelta(days=1)).isoformat()
            for i in range(100):
                db.execute("INSERT INTO banking_missions(id,session_id,title,status,input_json,nodes_json,digest,created_at,updated_at,not_before) VALUES(?,?,?,'RUNNING',?,'[]',?,'2000-01-01','2000-01-01',?)",(f'future-{i}',sid,'future',json.dumps({'not_before':later}),'0'*64,later))
        ready=m.prepare(m.MissionRequest(amount='100',minimum_balance='1000'),self.token)
        m.action(ready['id'],m.MissionAction(action='confirm',digest=ready['digest']),self.token)
        m.run_tick()
        actual=m.get_mission(ready['id'],self.token)
        self.assertEqual(next(n for n in actual['nodes'] if n['id']=='analyze')['status'],'COMPLETED')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_chinese_amounts_and_decimal_floor_are_preserved_without_float_rounding(self):
        chinese=self.propose('核对账单，预留五百元，至少保留一千元生活费')
        self.assertEqual(chinese['plan']['amount'],'500.00');self.assertEqual(chinese['plan']['minimum_balance'],'1000.00')
        exact=self.propose('核对账单，预留1500元，至少保留1000.01元')
        self.assertEqual(exact['status'],'BLOCKED')
        revised=self.edit(exact,amount='1499.99')
        self.assertEqual(revised['status'],'READY')

    def test_unchanged_values_keep_literal_provenance_when_only_floor_is_filled(self):
        proposal=self.propose('核对账单，预留500元，别影响房租')
        edited=self.edit(proposal,amount='500.00',minimum_balance='1000',execution_mode='immediate')
        self.assertEqual(edited['plan']['provenance']['amount']['source'],'text')
        self.assertEqual([c['field'] for c in edited['plan']['changes']],['minimum_balance'])

    def test_proposal_compile_is_atomic_and_idempotent_under_concurrency(self):
        proposal=self.ready()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.compile(proposal),range(2)))
        self.assertEqual(results[0]['id'],results[1]['id'])
        with b._connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_missions').fetchone()[0],1)

    def test_expired_or_cross_session_proposals_cannot_compile(self):
        proposal=self.ready();other=b.create_session()['token']
        with self.assertRaises(HTTPException):g.read(proposal['id'],other)
        with self.assertRaises(HTTPException):g.compile_goal(proposal['id'],g.Compile(digest=proposal['digest']),other)
        with b._connect() as db:db.execute("UPDATE banking_goal_proposals SET expires_at='2000-01-01' WHERE id=?",(proposal['id'],))
        with self.assertRaises(HTTPException):self.compile(proposal)

    def test_execution_time_cannot_outlive_current_account_session(self):
        proposal=self.ready()
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET expires_at=?',((b._now()+timedelta(hours=1)).isoformat(),))
        current=g.read(proposal['id'],self.token)
        self.assertEqual(current['status'],'BLOCKED');self.assertTrue(any('会话有效期' in s for s in current['issues']))
        with self.assertRaises(HTTPException):self.compile(proposal)

    def test_current_month_is_anchored_to_goal_even_when_compiled_after_midnight(self):
        import calendar
        now=b._now().astimezone(b.TZ)
        start=now.replace(day=calendar.monthrange(now.year,now.month)[1],hour=23,minute=55,second=0,microsecond=0)
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET expires_at=?',((start+timedelta(days=7)).isoformat(),))
        with patch.object(b,'_now',return_value=start):proposal=self.ready()
        later=start+timedelta(minutes=10)
        with patch.object(b,'_now',return_value=later),patch.object(m,'_now',return_value=later):mission=self.compile(proposal)
        self.assertEqual(mission['input']['bill_period'],start.strftime('%Y-%m'))
        self.assertNotEqual(mission['input']['bill_period'],later.strftime('%Y-%m'))

    def test_live_balance_change_is_rechecked_and_no_amount_is_silently_reduced(self):
        proposal=self.ready()
        with b._connect() as db:db.execute('UPDATE sandbox_sessions SET reserved_fen=200000')
        changed=g.read(proposal['id'],self.token);self.assertEqual(changed['status'],'BLOCKED')
        self.assertEqual(changed['plan']['amount'],'500.00');self.assertEqual(changed['plan']['minimum_balance'],'1000.00')
        with self.assertRaises(HTTPException):self.compile(proposal)

    def test_unknown_subscription_is_clarified_without_dumping_directory(self):
        proposal=self.propose('核对账单，预留500元，别影响房租，取消指定订阅')
        self.assertIn('subscription_name',{q['field'] for q in proposal['questions']})
        revised=self.edit(proposal,minimum_balance='1000',subscription_name='云上视频')
        self.assertEqual(revised['status'],'READY');self.assertEqual(revised['plan']['subscription_name'],'云上视频')
        self.assertNotIn('听书计划',json.dumps(revised,ensure_ascii=False))

    def test_edit_cannot_add_cancel_tool_and_rejects_naive_or_past_time(self):
        proposal=self.ready()
        with self.assertRaises(HTTPException):self.edit(proposal,subscription_name='云上视频')
        for when in ((b._now()+timedelta(days=1)).replace(tzinfo=None),b._now()-timedelta(days=1)):
            with self.assertRaises(HTTPException):self.edit(proposal,not_before=when,execution_mode='scheduled')

    def test_payload_tampering_fails_closed_and_original_text_is_encrypted(self):
        proposal=self.ready()
        from backend.banking_storage import raw,PREFIX
        with b._connect() as db:
            value=raw(db,'SELECT payload FROM banking_goal_proposals WHERE id=?',(proposal['id'],))[0][0]
            self.assertTrue(value.startswith(PREFIX))
            payload=proposal['plan'];payload['minimum_balance']='0.00'
            db.execute('UPDATE banking_goal_proposals SET payload=? WHERE id=?',(json.dumps(payload),proposal['id']))
        with self.assertRaises(HTTPException) as error:g.read(proposal['id'],self.token)
        self.assertEqual(error.exception.status_code,503)

    def test_complex_conversation_returns_durable_proposal_without_creating_mission(self):
        result=m.message(m.MessageRequest(text='核对账单，预留500元，别影响房租',client_id='goal-conversation-01'),self.token)
        self.assertEqual(result['attachment']['kind'],'goal_plan')
        repeated=m.message(m.MessageRequest(text='核对账单，预留500元，别影响房租',client_id='goal-conversation-01'),self.token)
        self.assertEqual(repeated['attachment'],result['attachment'])
        with b._connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_missions').fetchone()[0],0)
