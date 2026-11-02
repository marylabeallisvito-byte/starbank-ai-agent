import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from scripts import banking_delivery as delivery
from scripts.init_banking_demo import initialise
from scripts.launch_banking import preflight


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=delivery.ROOT/'backend/data');self.root=Path(self.temp.name)
        self.write('frontend/dist/index.html','<main>Local demo</main>')
        self.write('frontend/dist/build-meta.json',json.dumps({'files':{}}))
        self.write('backend/main.py','# synthetic source')
        self.write('scripts/launch_banking.py','# synthetic launcher')
        self.write('requirements.txt','fastapi==0.1\n')
        self.report={'source_sha256':'fixture-sha','regression':{'passed':1,'total':1},'intent':{'passed':1,'total':1},'interpretation_side_effects':{'no_funds_moved':True}}
        self.save_report()
        self.source=patch('backend.banking_readiness.source_hash',return_value='fixture-sha');self.source.start()
    def tearDown(self):self.source.stop();self.temp.cleanup()
    def write(self,name,content):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8')
    def save_report(self):self.write('backend/eval/reports/banking_release_v1.json',json.dumps(self.report))
    def unpack(self):
        result=delivery.build(self.root)
        folder=self.root/'output/extracted';folder.mkdir()
        with zipfile.ZipFile(result['archive']) as archive:archive.extractall(folder)
        return result,folder

    def test_release_excludes_local_secrets_database_and_research_data(self):
        self.write('.env','BANKING_OPERATOR_TOKEN=synthetic-private-value-123456789\n')
        self.write('backend/data/private/reviewer.token','synthetic-private-value-123456789')
        self.write('backend/data/banking.sqlite3','private research records')
        result,folder=self.unpack()
        self.assertFalse((folder/'.env').exists());self.assertFalse((folder/'backend/data').exists())
        self.assertTrue(delivery.verify(folder)['verified']);self.assertTrue(Path(result['archive']+'.sha256').exists())

    def test_secret_scan_blocks_embedded_configured_value_without_printing_it(self):
        secret='synthetic-private-value-123456789'
        self.write('.env','BANKING_OPERATOR_TOKEN='+secret)
        self.write('backend/accidental.py','TOKEN='+repr(secret))
        with self.assertRaises(ValueError) as error:delivery.build(self.root)
        self.assertNotIn(secret,str(error.exception));self.assertIn('configured_secret',str(error.exception))

    def test_failed_or_stale_report_and_missing_asset_block_packaging(self):
        self.report['regression']['passed']=0;self.save_report()
        with self.assertRaises(ValueError):delivery.build(self.root)
        self.report['regression']['passed']=1;self.report['source_sha256']='stale';self.save_report()
        with self.assertRaises(ValueError):delivery.build(self.root)
        self.report['source_sha256']='fixture-sha';self.save_report()
        self.write('frontend/dist/index.html','<script src="/assets/missing.js"></script>')
        with self.assertRaises(ValueError):delivery.build(self.root)

    def test_tampered_release_is_rejected_before_startup(self):
        _,folder=self.unpack();self.assertTrue(initialise(folder)['created'])
        self.assertTrue(preflight(folder)['verified'])
        (folder/'backend/main.py').write_text('# tampered',encoding='utf-8')
        with self.assertRaises(ValueError):preflight(folder)

    def test_initialisation_preserves_existing_configuration(self):
        self.write('.env','EXISTING=keep-me\n')
        self.assertFalse(initialise(self.root)['created'])
        self.assertEqual((self.root/'.env').read_text(encoding='utf-8'),'EXISTING=keep-me\n')

    def test_manifest_path_traversal_is_rejected(self):
        self.write(delivery.MANIFEST,json.dumps({'files':{'../outside':'bad'}}))
        with self.assertRaises(ValueError):delivery.verify(self.root)

    def test_stale_frontend_source_blocks_release(self):
        self.write('frontend/src/App.tsx','export default null')
        with self.assertRaises(ValueError):delivery.build(self.root)
