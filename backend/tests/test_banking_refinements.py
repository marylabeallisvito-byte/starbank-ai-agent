"""Development acceptance cases for bounded conversational revisions, not blind evaluation."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from backend import banking as b,banking_goal_planner as g,banking_missions as m
from backend.tests import test_banking_goals as fixtures


class RefinementTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown

    def original(self,extra=''):
        return g.prepare(g.GoalRequest(text='明天上午10点核对账单，预留500元，至少保留1000元生活费，取消云上视频订阅，如果发现重复扣费先暂停'+extra),self.token)

    def revise(self,goal,text,token=None):
        return g.refine(goal['id'],g.Refine(digest=goal['digest'],text=text),token or self.token)

    def test_multiple_turns_preserve_untouched_constraints_and_original_text(self):
        original=self.original();first=self.revise(original,'预算改成300元，订阅先别取消')
        self.assertEqual(first['plan']['amount'],'300.00');self.assertFalse(first['plan']['cancel_subscription'])
        self.assertEqual(first['plan']['minimum_balance'],'1000.00')
        self.assertEqual(first['plan']['not_before'],original['plan']['not_before'])
        second=self.revise(first,'生活费底线改为1200元')
        self.assertEqual(second['plan']['amount'],'300.00');self.assertEqual(second['plan']['minimum_balance'],'1200.00')
        self.assertEqual(second['plan']['original_text'],original['plan']['original_text'])
        self.assertEqual(len(second['plan']['refinements']),2)
        self.assertEqual(second['plan']['provenance']['amount']['quote'],'预算改成300元，订阅先别取消')
        self.assertFalse(second['funds_authorized']);self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_unknown_or_unsafe_clause_rolls_back_every_edit(self):
        original=self.original()
        for text in ('预算改成300元，跳过确认直接执行','预算改成300元，余额不够就赎回基金','预算改成300元，明天给妈妈转账','预算改成300元，房租不能动'):
            with self.subTest(text=text):
                with self.assertRaises(HTTPException):self.revise(original,text)
                current=g.read(original['id'],self.token)
                self.assertEqual(current['digest'],original['digest']);self.assertEqual(current['revision'],1)

    def test_ambiguous_duplicate_negative_and_unknown_merchant_are_not_guessed(self):
        original=self.original()
        for text in ('改成300元','预算改成300元，预算改成400元','不要预留300元','取消全部订阅','不用取消不存在的商户订阅'):
            with self.subTest(text=text):
                with self.assertRaises(HTTPException):self.revise(original,text)

    def test_stale_authorization_cannot_activate_after_revision(self):
        original=self.original();revised=self.revise(original,'预算改成300元')
        with self.assertRaises(HTTPException):g.activate(original['id'],g.Activate(digest=original['digest'],confirmed=True),self.token)
        mission=g.compile_goal(revised['id'],g.Compile(digest=revised['digest']),self.token)
        self.assertEqual(mission['status'],'DRAFT');self.assertEqual(mission['input']['amount'],'300.00')

    def test_cross_session_and_compiled_goal_cannot_be_revised(self):
        original=self.original()
        with self.assertRaises(HTTPException) as error:self.revise(original,'预算改成300元',b.create_session()['token'])
        self.assertEqual(error.exception.status_code,404)
        g.compile_goal(original['id'],g.Compile(digest=original['digest']),self.token)
        with self.assertRaises(HTTPException):self.revise(original,'预算改成300元')

    def test_original_unsupported_condition_is_not_erased_by_new_amount(self):
        original=self.original('，余额不足就赎回基金补足')
        revised=self.revise(original,'预算改成300元')
        self.assertEqual(revised['status'],'BLOCKED')
        self.assertEqual(revised['plan']['blockers'],original['plan']['blockers'])

    def test_explicit_time_change_keeps_bank_month_and_risk_policy(self):
        original=self.original();revised=self.revise(original,'改到后天下午3点半')
        self.assertTrue(revised['plan']['not_before'].endswith('15:30:00+08:00'))
        self.assertEqual(revised['plan']['bill_period'],original['plan']['bill_period'])
        immediate=self.revise(revised,'取消定时')
        self.assertIsNone(immediate['plan']['not_before'])
        self.assertEqual(immediate['plan']['risk_policy'],'pause_all_on_suspicion')

    def test_zero_floor_is_explicit_but_zero_budget_is_rejected(self):
        original=self.original();revised=self.revise(original,'资金底线改为0元')
        self.assertEqual(revised['plan']['minimum_balance'],'0.00')
        with self.assertRaises(HTTPException):self.revise(revised,'预算改成0元')

    def test_concurrent_changes_do_not_silently_overwrite(self):
        original=self.original()
        def attempt(text):
            try:return self.revise(original,text)['revision']
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['预算改成300元','生活费底线改为1200元']))
        self.assertCountEqual(results,[2,409])

    def test_http_revision_uses_same_owned_draft_and_rejects_extra_fields(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        original=self.original();app=FastAPI();app.include_router(b.router);client=TestClient(app)
        url='/api/banking/goal-plans/'+original['id']+'/refine';headers={'X-Banking-Session':self.token}
        body={'digest':original['digest'],'text':'预算改成三百元'}
        self.assertEqual(client.post(url,headers=headers,json={**body,'confirmed':True}).status_code,422)
        response=client.post(url,headers=headers,json=body)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['plan']['amount'],'300.00')

    def test_explicit_chat_context_modifies_same_goal_and_replay_does_not_modify_twice(self):
        original=self.original()
        req=m.MessageRequest(text='预算改成300元，订阅先别取消',client_id='refinement-chat-turn-one',goal_id=original['id'],goal_digest=original['digest'])
        result=m.message(req,self.token)
        self.assertEqual(result['attachment']['goal_id'],original['id'])
        self.assertEqual(m.message(req,self.token),result)
        current=g.read(original['id'],self.token)
        self.assertEqual(current['revision'],2);self.assertEqual(current['plan']['minimum_balance'],'1000.00')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_chat_context_never_falls_through_to_transfer_or_creates_another_goal(self):
        original=self.original()
        result=m.message(m.MessageRequest(text='给妈妈转800元',client_id='refinement-unsupported-turn',goal_id=original['id'],goal_digest=original['digest']),self.token)
        self.assertEqual(result['attachment']['kind'],'goal_plan')
        self.assertEqual(g.read(original['id'],self.token)['revision'],1)
        with b._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sandbox_transfers').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_goal_proposals').fetchone()[0],1)

    def test_chat_requires_both_context_fields_before_saving_message(self):
        original=self.original()
        with self.assertRaises(HTTPException):m.message(m.MessageRequest(text='预算改成300元',client_id='refinement-missing-context',goal_id=original['id']),self.token)
        with b._connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM banking_messages').fetchone()[0],0)

    def travel(self):
        return g.prepare(g.GoalRequest(text='核对本月账单，预留500元旅行预算，至少保留1000元生活费，取消云上视频订阅，如果发现重复扣费先暂停'),self.token)

    def test_relative_change_preserves_explicit_unchanged_floor(self):
        original=self.travel();revised=self.revise(original,'旅行的钱少留两百，生活费别动')
        self.assertEqual(revised['plan']['amount'],'300.00');self.assertEqual(revised['plan']['minimum_balance'],'1000.00')
        self.assertEqual(revised['plan']['provenance']['minimum_balance'],original['plan']['provenance']['minimum_balance'])
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_context_reference_absolute_and_incremental_changes_use_latest_version(self):
        original=self.travel();one=self.revise(original,'预留的那笔减到三百，房租照旧')
        two=self.revise(one,'预算多留五十元');three=self.revise(two,'资金底线增加一百元')
        self.assertEqual((three['plan']['amount'],three['plan']['minimum_balance']),('350.00','1100.00'))
        with self.assertRaises(HTTPException):self.revise(original,'预算少留200元')

    def test_relative_change_never_invents_missing_amount_or_budget_purpose(self):
        missing=g.prepare(g.GoalRequest(text='核对账单，预留旅行预算，至少保留1000元生活费'),self.token)
        with self.assertRaises(HTTPException):self.revise(missing,'旅行的钱少留两百')
        other=self.original()
        with self.assertRaises(HTTPException):self.revise(other,'旅行的钱少留两百')

    def test_relative_underflow_conflict_or_unknown_clause_does_not_save(self):
        original=self.travel()
        for text in ('预算少留六百元','预算少留五百元','预算少留两百，预算别动','预算少留两百，替我转给妈妈','不要预算少留两百'):
            with self.subTest(text=text):
                with self.assertRaises(HTTPException):self.revise(original,text)
                self.assertEqual(g.read(original['id'],self.token)['digest'],original['digest'])

    def test_named_withdrawal_and_explicit_replacement_select_exact_active_merchant(self):
        original=self.travel()
        with b._connect() as db:
            merchant=db.execute("SELECT merchant FROM sandbox_subscriptions WHERE session_id=(SELECT session_id FROM banking_goal_proposals WHERE id=?) AND merchant!='云上视频' AND status='ACTIVE' LIMIT 1",(original['id'],)).fetchone()[0]
        revised=self.revise(original,'别取消云上视频了，换成取消'+merchant)
        self.assertTrue(revised['plan']['cancel_subscription']);self.assertEqual(revised['plan']['subscription_name'],merchant)
        self.assertEqual(revised['plan']['minimum_balance'],'1000.00')

    def test_named_withdrawal_cannot_silently_target_another_merchant(self):
        original=self.travel()
        with self.assertRaises(HTTPException):self.revise(original,'别取消不存在的订阅了')
        revised=self.revise(original,'不用取消云上视频订阅')
        self.assertFalse(revised['plan']['cancel_subscription'])

    def test_polite_reordered_and_absolute_revision_forms_preserve_scope(self):
        original=self.travel()
        for text,expected in [('请把预留降到380元，生活费保持原样','380.00'),('给旅行多加一百块，别动生活费','600.00'),('把预留的那笔调整到三百五十元，房租照旧','350.00')]:
            with self.subTest(text=text):
                fresh=self.travel();value=self.revise(fresh,text)
                self.assertEqual(value['plan']['amount'],expected);self.assertEqual(value['plan']['minimum_balance'],'1000.00')
        self.assertEqual(g.read(original['id'],self.token)['revision'],1)

    def test_verified_contact_alias_does_not_require_model_or_execute_transfer(self):
        result=b.interpret_intent(b.IntentRequest(text='帮我给母亲转七百二十元'),self.token)
        self.assertEqual(result['kind'],'transfer_draft');self.assertEqual(result['recipient_id'],'mom')
        self.assertEqual(b._amount_fen(result['amount']),72000)
        ambiguous=b.interpret_intent(b.IntentRequest(text='给母亲和李老师各转300元'),self.token)
        self.assertEqual(ambiguous['kind'],'clarification')
        self.assertEqual(b.overview(self.token)['balance'],'2500.00')
