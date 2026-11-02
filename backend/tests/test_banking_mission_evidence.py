"""Actual task sources, strict ownership, integrity and erasure propagation."""
import json
import unittest
from fastapi import HTTPException
from backend.tests import test_banking_planning as fixtures
from backend import banking as b, banking_missions as m, banking_mission_evidence as e, banking_statements as statements


class MissionEvidenceTests(unittest.TestCase):
    setUp=fixtures.PlanningTests.setUp
    tearDown=fixtures.PlanningTests.tearDown
    import_rows=fixtures.PlanningTests.import_rows
    confirm=fixtures.PlanningTests.confirm

    def analyzed(self,source='sandbox'):
        plan=m.prepare(m.MissionRequest(amount='500',bill_source=source),self.token)
        self.confirm(plan)
        m.tick(plan['id'])
        return plan

    def read(self,plan,token=None): return e.mission_evidence(plan['id'],0,token or self.token)

    def test_task_graph_only_contains_actual_cited_records(self):
        plan=self.analyzed()
        result=self.read(plan)
        self.assertEqual(result['status'],'available')
        self.assertEqual({r['id'] for r in result['records']},set(result['finding']['transaction_ids']))
        self.assertTrue(all(r['current_status']=='matching' for r in result['records']))
        self.assertFalse(any(edge['type']=='TRANSFER' for edge in result['network']['graph']['edges']))
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')

    def test_imported_record_has_original_batch_and_file_digest(self):
        batch=self.import_rows(); plan=self.analyzed('imported'); result=self.read(plan)
        self.assertTrue(all(r['batch_id']==batch['id'] for r in result['records']))
        self.assertTrue(all(len(r['file_hash'])==64 for r in result['records']))
        self.assertEqual(result['source'],'imported')

    def test_cross_session_cannot_read_task_provenance(self):
        plan=self.analyzed()
        with self.assertRaises(HTTPException) as error: self.read(plan,b.create_session()['token'])
        self.assertEqual(error.exception.status_code,404)

    def test_modified_source_is_distinguished_from_integrity_failure(self):
        plan=self.analyzed()
        result=self.read(plan)
        with b._connect() as db: db.execute('UPDATE sandbox_bills SET amount_fen=amount_fen+1 WHERE id=?',(result['records'][0]['id'],))
        changed=self.read(plan)
        self.assertEqual(changed['status'],'changed')
        self.assertIn('changed',[r['current_status'] for r in changed['records']])
        with b._connect() as db: db.execute("UPDATE banking_mission_sources SET payload='{}'")
        with self.assertRaises(HTTPException) as error: self.read(plan)
        self.assertEqual(error.exception.status_code,503)

    def test_erasure_redacts_derived_evidence_and_preserves_other_sessions(self):
        batch=self.import_rows(); plan=self.analyzed('imported')
        statements.erase(batch['id'],statements.Erase(confirmed=True),self.token)
        result=self.read(plan)
        self.assertEqual(result['status'],'changed')
        self.assertTrue(all(r.get('redacted') for r in result['records']))
        self.assertTrue(all('merchant' not in r and 'amount' not in r for r in result['records']))
        with b._connect() as db:
            saved=db.execute('SELECT payload FROM banking_mission_sources WHERE mission_id=?',(plan['id'],)).fetchone()[0]
            self.assertNotIn('测试商户',saved)
        self.assertEqual(result['finding']['merchant'],'来源已清除')

    def test_no_snapshot_is_not_fabricated_for_draft_or_historical_task(self):
        plan=m.prepare(m.MissionRequest(amount='500'),self.token)
        self.assertEqual(self.read(plan)['status'],'pending')
        self.confirm(plan); m.tick(plan['id'])
        with b._connect() as db: db.execute('DELETE FROM banking_mission_sources')
        self.assertEqual(self.read(plan)['status'],'legacy_unavailable')

    def test_new_source_change_after_analysis_blocks_budget_before_mutation(self):
        with b._connect() as db: db.execute('DELETE FROM sandbox_bills')
        plan=self.analyzed()
        m.tick(plan['id']); m.tick(plan['id'])
        with b._connect() as db:
            sid=db.execute('SELECT session_id FROM banking_missions WHERE id=?',(plan['id'],)).fetchone()[0]
            db.execute('INSERT INTO sandbox_bills(id,session_id,merchant,category,amount_fen,occurred_at) VALUES(?,?,?,?,?,?)',('new-source',sid,'新增测试','其他',100,b._now().isoformat()))
        m.tick(plan['id'])
        self.assertEqual(m.list_missions(self.token)['items'][0]['status'],'BLOCKED')
        self.assertEqual(b.overview(self.token)['reserved'],'0.00')
