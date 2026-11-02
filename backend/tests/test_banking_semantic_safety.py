"""Development regression: prohibitions and unobservable prerequisites must survive."""
import unittest
from fastapi import HTTPException
from backend import banking as b, banking_goal_planner as g
from backend.tests import test_banking_goals as fixtures


class SemanticSafetyTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown

    def prepare(self,extra):
        return g.prepare(g.GoalRequest(text='核对账单，预留500元，至少保留1000元生活费，'+extra),self.token)

    def test_prohibitions_never_select_subscription_cancellation(self):
        for prefix in ('不得','不允许','禁止','不准','不要','无需','不用','不必'):
            with self.subTest(prefix=prefix):
                value=self.prepare(prefix+'取消云上视频订阅')
                self.assertEqual(value['status'],'READY')
                self.assertFalse(value['plan']['cancel_subscription'])
                self.assertIsNone(value['plan']['subscription_id'])
                mission=g.compile_goal(value['id'],g.Compile(digest=value['digest']),self.token)
                self.assertFalse(mission['input']['cancel_subscription'])
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_postposed_prohibition_blocks_compilation(self):
        value=self.prepare('取消云上视频订阅是不允许的')
        self.assertEqual(value['status'],'BLOCKED')
        with self.assertRaises(HTTPException):g.compile_goal(value['id'],g.Compile(digest=value['digest']),self.token)

    def test_unobservable_prerequisite_is_not_silently_dropped_even_after_edit(self):
        for condition in ('仅当工资到账时办理','只有爱人同意才办理','必须经过爱人同意','确认到账才执行','等工资到账再办理','须经家人确认'):
            with self.subTest(condition=condition):
                value=self.prepare(condition)
                self.assertEqual(value['status'],'BLOCKED')
                self.assertTrue(any(condition==v['quote'] for v in value['plan']['blockers']))
                edited=g.refine(value['id'],g.Refine(digest=value['digest'],text='预算改成400元'),self.token)
                self.assertEqual(edited['status'],'BLOCKED')
                with self.assertRaises(HTTPException):g.activate(edited['id'],g.Activate(digest=edited['digest'],confirmed=True),self.token)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_prohibition_edit_invalidates_old_digest_and_removes_operation(self):
        original=self.prepare('取消云上视频订阅')
        changed=g.refine(original['id'],g.Refine(digest=original['digest'],text='预算改成400元，不得取消云上视频订阅'),self.token)
        self.assertFalse(changed['plan']['cancel_subscription'])
        self.assertIsNone(changed['plan']['subscription_id'])
        with self.assertRaises(HTTPException):g.activate(original['id'],g.Activate(digest=original['digest'],confirmed=True),self.token)

    def test_unknown_prohibited_merchant_rolls_back_budget_change(self):
        original=self.prepare('取消云上视频订阅')
        with self.assertRaises(HTTPException):g.refine(original['id'],g.Refine(digest=original['digest'],text='预算改成400元，禁止取消不存在的订阅'),self.token)
        self.assertEqual(g.read(original['id'],self.token)['digest'],original['digest'])
