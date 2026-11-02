import os
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi import HTTPException
from backend.tests import test_banking_planning as fixtures
from backend import banking as b,banking_missions as m,banking_storage as s


class StorageTests(unittest.TestCase):
    import_rows=fixtures.PlanningTests.import_rows

    def setUp(self):
        self.key=Fernet.generate_key().decode()
        self.crypto=patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':self.key,'BANKING_DATA_PREVIOUS_KEYS':''})
        self.crypto.start()
        fixtures.PlanningTests.setUp(self)

    def tearDown(self):
        try:fixtures.PlanningTests.tearDown(self)
        finally:self.crypto.stop()

    def test_records_drafts_tasks_audits_and_messages_are_encrypted_on_disk(self):
        self.import_rows()
        plan=m.prepare(m.MissionRequest(amount='500',bill_source='imported'),self.token)
        m.message(m.MessageRequest(text='Sensitive-ALPHA-192',client_id='storage-pilot-192'),self.token)
        with b._connect() as db:
            self.assertEqual(m.view(db,m.owned(db,db.execute('SELECT id FROM sandbox_sessions LIMIT 1').fetchone()[0],plan['id']))['input']['amount'],'500.00')
            self.assertIn('测试商户',db.execute('SELECT payload FROM statement_records LIMIT 1').fetchone()[0])
        with closing(sqlite3.connect(b.DB_PATH)) as db:
            for table,column in [('statement_records','payload'),('statement_drafts','rows_json'),('banking_missions','input_json'),('banking_messages','text'),('sandbox_audit','detail')]:
                self.assertTrue(db.execute(f'SELECT {column} FROM {table} LIMIT 1').fetchone()[0].startswith(s.PREFIX))
        self.assertNotIn(b'Sensitive-ALPHA-192',b.DB_PATH.read_bytes())

    def test_old_plaintext_is_migrated_and_logical_audit_remains_valid(self):
        with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':'','BANKING_DATA_PREVIOUS_KEYS':''}):
            # A separate legacy file avoids weakening an already-encrypted database.
            original=b.DB_PATH
            with patch.object(b,'DB_PATH',original.with_name('legacy.sqlite3')):
                token=b.create_session()['token']
                b.prepare_transfer(b.PrepareTransfer(recipient_id='mom',amount='80',note='Legacy-Sensitive-ALPHA'),token)
        with patch.object(b,'DB_PATH',original.with_name('legacy.sqlite3')):
            with b._connect() as db:
                self.assertEqual(db.execute('SELECT note FROM sandbox_transfers').fetchone()[0],'Legacy-Sensitive-ALPHA')
            self.assertNotIn(b'Legacy-Sensitive-ALPHA',b.DB_PATH.read_bytes())
            self.assertTrue(b.audit(token)['integrity'])

    def test_missing_or_wrong_key_fails_closed(self):
        for key in ['',Fernet.generate_key().decode()]:
            with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':key,'BANKING_DATA_PREVIOUS_KEYS':''}):
                with self.assertRaises(HTTPException) as error:
                    with b._connect():pass
                self.assertEqual(error.exception.status_code,503)

    def test_rotation_with_previous_key_preserves_records_and_removes_old_dependency(self):
        self.import_rows()
        new=Fernet.generate_key().decode()
        with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':new,'BANKING_DATA_PREVIOUS_KEYS':self.key}):
            with b._connect() as db:
                self.assertIn('测试商户',db.execute('SELECT payload FROM statement_records LIMIT 1').fetchone()[0])
        with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':new,'BANKING_DATA_PREVIOUS_KEYS':''}):
            with b._connect() as db:
                self.assertIn('测试商户',db.execute('SELECT payload FROM statement_records LIMIT 1').fetchone()[0])
        with patch.dict(os.environ,{'BANKING_DATA_ENCRYPTION_KEY':self.key,'BANKING_DATA_PREVIOUS_KEYS':''}):
            with self.assertRaises(HTTPException):
                with b._connect():pass

    def test_corrupted_ciphertext_is_not_rendered_as_plaintext(self):
        self.import_rows()
        with closing(sqlite3.connect(b.DB_PATH)) as db:
            db.execute('DROP TRIGGER bank_enc_statement_records_update')
            db.execute('UPDATE statement_records SET payload=?',(s.PREFIX+'invalid:tampered',));db.commit()
        with self.assertRaises(HTTPException):
            with b._connect() as db:db.execute('SELECT payload FROM statement_records').fetchall()
