import unittest
from fastapi import HTTPException
from backend.tests import test_banking_storage as fixtures
from backend import banking as b,banking_research as r


class ResearchTests(unittest.TestCase):
    setUp=fixtures.StorageTests.setUp
    tearDown=fixtures.StorageTests.tearDown

    def enroll(self,code='DEV01',cohort='developer_pilot',protocol=r.VERSION):
        return r.enroll(r.Consent(participant_code=code,cohort=cohort,confirmed=True,protocol=protocol))['token']

    def test_consent_and_trial_ownership_are_required(self):
        with self.assertRaises(HTTPException):r.enroll(r.Consent(participant_code='DEV01',cohort='developer_pilot',confirmed=False))
        one=self.enroll();two=self.enroll('DEV02')
        trial=r.start(one)
        self.assertEqual(r.start(one)['id'],trial['id'])
        with self.assertRaises(HTTPException):r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=4,confidence=4),two)

    def test_developer_pilot_never_counts_as_external_user(self):
        token=self.enroll()
        for _ in range(6):
            trial=r.start(token)
            r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=7,confidence=7,comment='Developer-Pilot-Sensitive-Only'),token)
        report=r.report()
        self.assertEqual(report['developer_pilot_sessions'],1)
        self.assertEqual(report['completed_external_sessions'],0)
        self.assertIsNone(report['metrics'])
        self.assertNotIn(b'Developer-Pilot-Sensitive-Only',b.DB_PATH.read_bytes())

    def test_withdrawal_removes_own_records_and_revokes_research_token(self):
        one=self.enroll();two=self.enroll('DEV02')
        r.start(one);r.start(two)
        self.assertTrue(r.withdraw(r.Withdraw(confirmed=True),one)['cleared'])
        with self.assertRaises(HTTPException):r.session(one)
        self.assertIsNotNone(r.session(two)['active'])

    def test_arm_order_is_counterbalanced_and_repeat_completion_rejected(self):
        one=self.enroll();two=self.enroll('DEV02')
        self.assertNotEqual(r.session(one)['arm_order'],r.session(two)['arm_order'])
        trial=r.start(one);payload=r.Response(trial_id=trial['id'],outcome='stopped',ease=2,confidence=2)
        r.complete(payload,one)
        with self.assertRaises(HTTPException):r.complete(payload,one)

    def test_monthly_protocol_has_four_owned_tasks_with_comprehension_and_reviewer_step(self):
        token=self.enroll(protocol=r.MONTHLY_VERSION);seen=[]
        self.assertEqual(r.session(token)['total'],4)
        for index in range(4):
            trial=r.start(token);seen.append(trial['task_id'])
            self.assertEqual(trial['arm'],'agent');self.assertTrue(trial['comprehension_question'])
            self.assertEqual(trial['reviewer_step'],index==3)
            r.complete(r.Response(trial_id=trial['id'],outcome='stopped',ease=3,confidence=2,comprehension='unsure'),token)
        self.assertEqual(seen,[task['id'] for task in r.MONTHLY_TASKS])
        with self.assertRaises(HTTPException):r.start(token)
        self.assertIsNone(r.report(r.MONTHLY_VERSION)['metrics'])

    def test_monthly_results_never_mix_with_original_protocol(self):
        legacy=self.enroll('DEV01');monthly=self.enroll('DEV01',protocol=r.MONTHLY_VERSION)
        self.assertEqual(r.session(legacy)['total'],6);self.assertEqual(r.session(monthly)['total'],4)
        self.assertEqual(r.report()['developer_pilot_sessions'],1)
        self.assertEqual(r.report(r.MONTHLY_VERSION)['developer_pilot_sessions'],1)

    def test_monthly_answers_are_required_but_never_claim_verified_execution(self):
        token=self.enroll(protocol=r.MONTHLY_VERSION);trial=r.start(token)
        with self.assertRaises(HTTPException):r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=5,confidence=5),token)
        self.assertEqual(r.session(token)['completed'],0)
        result=r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=5,confidence=5,comprehension='no',comment='Monthly-private-feedback'),token)
        self.assertFalse(result['success_verified_by_server'])
        self.assertNotIn(b'Monthly-private-feedback',b.DB_PATH.read_bytes())

    def test_monthly_report_is_single_arm_and_separates_extra_help_from_self_reported_success(self):
        for person in range(3):
            token=self.enroll('SYNTH'+str(person),'external_participant',r.MONTHLY_VERSION)
            for index in range(4):
                trial=r.start(token)
                r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=5,confidence=4,comprehension='no',assistance='protocol_reviewer' if index==3 else 'extra_help' if index==1 else 'none'),token)
        monthly=r.report(r.MONTHLY_VERSION)
        self.assertEqual(monthly['comparison_design'],'single_arm_monthly_tasks')
        self.assertEqual(monthly['completed_external_sessions'],3)
        self.assertEqual(set(monthly['metrics']),{'agent'})
        self.assertEqual(monthly['metrics']['agent']['comprehension_answer_accuracy'],0.75)
        self.assertEqual(monthly['metrics']['agent']['extra_help_trial_rate'],0.25)
        self.assertEqual(r.report()['completed_external_sessions'],0)

    def test_monthly_restore_cannot_be_reported_as_unassisted_success(self):
        token=self.enroll(protocol=r.MONTHLY_VERSION)
        for _ in range(3):
            trial=r.start(token);r.complete(r.Response(trial_id=trial['id'],outcome='stopped',ease=4,confidence=4,comprehension='unsure'),token)
        trial=r.start(token)
        with self.assertRaises(HTTPException):r.complete(r.Response(trial_id=trial['id'],outcome='success',ease=4,confidence=4,comprehension='no'),token)

    def test_monthly_withdrawal_does_not_erase_other_protocol(self):
        legacy=self.enroll('DEV01');monthly=self.enroll('DEV01',protocol=r.MONTHLY_VERSION)
        r.start(legacy);r.start(monthly)
        r.withdraw(r.Withdraw(confirmed=True),monthly)
        self.assertIsNotNone(r.session(legacy)['active'])
        self.assertEqual(r.report(r.MONTHLY_VERSION)['developer_pilot_sessions'],0)
