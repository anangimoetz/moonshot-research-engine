import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from moonshot_scanner.supervise import preflight
from moonshot_scanner.app import create_app
from moonshot_scanner.config import Settings

class ReleaseTests(unittest.TestCase):
    def test_source_off_needs_no_telegram_secret(self):
        preflight({'SCANNER_TOKEN':'local-test-only'})
    def test_missing_dashboard_token_rejected(self):
        with self.assertRaises(ValueError):preflight({})
    def test_source_cannot_start_without_authorization(self):
        with self.assertRaises(ValueError):preflight({'SCANNER_TOKEN':'local-test-only','SCANNER_ROSE_ENABLED':'1'})
    def test_no_session_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            env={'SCANNER_TOKEN':'local-test-only','SCANNER_ROSE_ENABLED':'1','ROSE_STATE_DIR':d}
            env.update({x:'test-value' for x in ['ROSE_TG_API_ID','ROSE_TG_API_HASH','ROSE_BOT_TOKEN','ROSE_CHAT_ID']})
            with self.assertRaises(ValueError):preflight(env)
            Path(d,'rose-source.session').touch();preflight(env)
    def test_https_login_behind_explicit_trusted_proxy(self):
        with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{'SCANNER_TRUST_PROXY':'1'}):
            app=create_app(Settings(state_dir=Path(d),token='test-only',session_reports=False))
            app.testing=True
            r=app.test_client().post('/api/login',json={'token':'test-only'},headers={'Origin':'https://localhost','X-Forwarded-Proto':'https','X-Forwarded-Host':'localhost'})
            self.assertEqual(r.status_code,200)
    def test_configuration_only_is_not_end_to_end_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=Settings(state_dir=Path(d),token='test-only',persistent_storage=True,auto_scan=True,bot_token='not-real',chat_id='not-real')
            app=create_app(cfg);app.testing=True
            store=app.extensions['scanner_store']
            from moonshot_scanner.store import utc_now
            store.set_state('worker',{'status':'RUNNING','heartbeat_at':utc_now()})
            r=app.test_client().get('/api/health',headers={'Authorization':'Bearer test-only'}).get_json()
            self.assertFalse(r['ready_for_24_7']);self.assertEqual(r['rose']['status'],'NOT_CONNECTED')
    def test_retention_compacts_candles_but_keeps_last_asset_chart(self):
        from moonshot_scanner.store import Store
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'s.sqlite3')
            for i in range(6):
                s.add_snapshot(str(i),{'exchange':'bitget','symbol':'TESTUSDT','market':{'candles':{'4h':[{'close':i}]}},'features':{'retained':i}})
            s.maintain(full_count=2,history_count=4)
            self.assertIsNone(s.job('not-a-job'))
            self.assertIn('candles',s.latest()[0]['market'])
            with s.connection() as db:
                import json
                rows=[json.loads(x[0]) for x in db.execute('SELECT data FROM snapshots ORDER BY id')]
            self.assertEqual(len(rows),4)
            self.assertNotIn('candles',rows[0]['market']);self.assertIn('retained',rows[0]['features'])
            self.assertIn('candles',rows[-1]['market'])

if __name__=='__main__':unittest.main()
