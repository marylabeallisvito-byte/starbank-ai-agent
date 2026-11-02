"""Real subprocess termination around commit; not physical power-loss simulation."""
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from backend import banking as b,banking_missions as m
from backend.tests import test_banking_goals as fixtures


class CrashRecoveryTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown

    def mission(self,complete=False):
        with b._connect() as db:db.execute('DELETE FROM sandbox_bills')
        plan=m.prepare(m.MissionRequest(amount='500',minimum_balance='1000'),self.token)
        m.action(plan['id'],m.MissionAction(action='confirm',digest=plan['digest']),self.token)
        for _ in range(8 if complete else 3):m.tick(plan['id'])
        return plan

    def crash(self,plan,operation,phase):
        env={**os.environ,'BANKING_SANDBOX_DB':str(b.DB_PATH),'BANKING_AUTH_MODE':'demo','BANKING_LLM_ENABLED':'0'}
        request={'mission_id':plan['id'],'operation':operation,'phase':phase,'customer_token':self.token}
        result=subprocess.run([sys.executable,'-m','backend.eval.banking_crash_worker'],input=json.dumps(request),text=True,capture_output=True,env=env,cwd=Path(__file__).resolve().parents[2],timeout=30)
        self.assertEqual(result.returncode,71 if phase=='before_commit' else 72,result.stderr[-1000:])

    def reservation(self,plan):
        with b._connect() as db:
            row=db.execute('SELECT * FROM banking_missions WHERE id=?',(plan['id'],)).fetchone()
            node=next(n for n in json.loads(row['nodes_json']) if n['id']=='reserve')
            count=sum(json.loads(event['detail']).get('node_id')=='reserve' for event in db.execute("SELECT detail FROM banking_mission_events WHERE mission_id=? AND event='NODE_CHECKPOINT'",(plan['id'],)))
            return b.overview(self.token)['reserved'],node['status'],count

    def test_exit_before_reservation_commit_rolls_back_cash_node_and_event(self):
        plan=self.mission();self.crash(plan,'reserve','before_commit')
        self.assertEqual(self.reservation(plan),('0.00','PENDING',0))
        for _ in range(6):m.tick(plan['id'])
        self.assertEqual(self.reservation(plan),('500.00','COMPLETED',1))

    def test_exit_after_reservation_commit_restarts_without_double_reservation(self):
        plan=self.mission();self.crash(plan,'reserve','after_commit')
        self.assertEqual(self.reservation(plan),('500.00','COMPLETED',1))
        for _ in range(6):m.tick(plan['id'])
        self.assertEqual(self.reservation(plan),('500.00','COMPLETED',1))

    def test_exit_during_release_rolls_back_then_release_can_be_retried(self):
        plan=self.mission(complete=True);self.crash(plan,'release','before_commit')
        self.assertEqual(b.overview(self.token)['reserved'],'500.00')
        self.assertFalse(m.get_mission(plan['id'],self.token)['released'])
        m.action(plan['id'],m.MissionAction(action='release'),self.token)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_exit_after_release_commit_and_retry_does_not_release_twice(self):
        plan=self.mission(complete=True);self.crash(plan,'release','after_commit')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')
        m.action(plan['id'],m.MissionAction(action='release'),self.token)
        with b._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM banking_mission_events WHERE mission_id=? AND event='MISSION_BUDGET_COMPENSATED'",(plan['id'],)).fetchone()[0],1)
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_encrypted_database_backup_restores_checkpoint_and_requires_original_key(self):
        plan=self.mission(complete=True)
        backup=b.DB_PATH.with_name('restored.sqlite3')
        with closing(sqlite3.connect(b.DB_PATH)) as source,closing(sqlite3.connect(backup)) as target:source.backup(target)
        m.action(plan['id'],m.MissionAction(action='release'),self.token)
        with patch.object(b,'DB_PATH',backup):
            self.assertEqual(b.overview(self.token)['reserved'],'500.00')
            self.assertEqual(m.get_mission(plan['id'],self.token)['status'],'COMPLETED')
            with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':'','BANKING_DATA_PREVIOUS_KEYS':''}):
                with self.assertRaises(HTTPException) as error:m.get_mission(plan['id'],self.token)
                self.assertEqual(error.exception.status_code,503)
            m.action(plan['id'],m.MissionAction(action='release'),self.token)
            self.assertEqual(b.overview(self.token)['reserved'],'0.00')
