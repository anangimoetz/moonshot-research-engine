import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from moonshot_scanner.chart_data import normalize,history
from moonshot_scanner.app import create_app
from moonshot_scanner.config import Settings

class ChartTests(unittest.TestCase):
    def test_open_candle_never_promoted_closed(self):
        x=normalize([[60000,10,12,9,11]],'1m',90000)[0]
        self.assertFalse(x['is_closed']);self.assertEqual(x['close_time'],120000)
    def test_utc_daily_alignment(self):
        self.assertEqual(normalize([[16*3600000,10,12,9,11]],'1d',2*86400000),[])
    def test_invalid_geometry_boolean_future_removed(self):
        for row in [[60000,True,12,9,11],[60000,10,9,8,11],[180000,10,12,9,11]]:
            self.assertEqual(normalize([row],'1m',90000),[])
    def test_sort_dedup(self):
        x=normalize([[120000,10,12,9,11],[60000,10,12,9,11],[60000,10,13,9,12]],'1m',200000)
        self.assertEqual([r['open_time'] for r in x],[60000,120000]);self.assertEqual(x[0]['close'],12)
    def test_invalid_inputs_do_not_fetch(self):
        for ex,sym,prod,tf in [('other','BTCUSDT','SPOT','1m'),('bitget','../private','SPOT','1m'),('bitget','BTCUSDT','ACCOUNT','1m'),('bitget','BTCUSDT','SPOT','1s')]:
            with self.assertRaises(ValueError):history(ex,sym,prod,tf,http_get=lambda *a,**k:self.fail('network called'))
    def test_each_venue_history_schema(self):
        row=[60000,'10','12','9','11']
        for ex,payload in [('bitget',{'code':'00000','data':[row]}),('binance',[row]),('bybit',{'retCode':0,'result':{'list':[row]}})]:
            data=history(ex,'BTCUSDT','SPOT','1m',http_get=lambda *a,**k:payload)
            self.assertEqual(data['status'],'FRESH_HISTORY');self.assertEqual(data['candles'][0]['close'],11)
    def test_daily_bitget_uses_explicit_utc_endpoint(self):
        urls=[]
        def get(url,**kw):urls.append(url);return {'code':'00000','data':[[0,10,12,9,11]]}
        history('bitget','BTCUSDT','SPOT','1d',http_get=get)
        self.assertIn('1Dutc',urls[0]);self.assertIn('/api/v2/spot/',urls[0])
    def test_chart_endpoint_auth_and_research_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app(Settings(state_dir=Path(d),token='test-only'));app.testing=True;c=app.test_client()
            self.assertEqual(c.get('/api/chart/history').status_code,401)
            with patch('moonshot_scanner.chart_data.history',return_value={'candles':[{}],'scope':'CHART_ONLY_NOT_ENTRY_CONFIRMATION'}):
                r=c.get('/api/chart/history?symbol=BTCUSDT',headers={'Authorization':'Bearer test-only'})
                self.assertEqual(r.status_code,200)
            self.assertEqual(app.extensions['scanner_store'].latest(),[])
    def test_required_telegram_fails_without_secret(self):
        with self.assertRaises(ValueError):Settings(require_telegram=True,bot_token='',chat_id='6720326248').validate()

if __name__=='__main__':unittest.main()
