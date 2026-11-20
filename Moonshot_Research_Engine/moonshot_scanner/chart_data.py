"""Fresh venue-specific chart history, kept separate from scanner decisions."""
import re
import time
import threading
from .providers import PublicMarketData, BITGET, BINANCE, BINANCE_PERP, BYBIT, _url
from .pipeline import number

INTERVALS={'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000,'1d':86400000}
_providers={}
_lock=threading.Lock()

def normalize(rows,interval,now_ms):
    ms=INTERVALS[interval];bars={}
    for row in rows or []:
        if not isinstance(row,(list,tuple)) or len(row)<5:continue
        values=[number(x) for x in row[:5]]
        if any(x is None for x in values):continue
        start,o,h,l,c=values;start=int(start)
        if start%ms or start>now_ms+5000 or min(o,h,l,c)<=0 or not l<=min(o,c)<=max(o,c)<=h:continue
        bars[start]={'open_time':start,'close_time':start+ms,'open':o,'high':h,'low':l,'close':c,'is_closed':start+ms<=now_ms}
    return [bars[k] for k in sorted(bars)]

def history(exchange,symbol,product,interval,http_get=None):
    if exchange not in {'bitget','binance','bybit'} or product not in {'SPOT','USDT-FUTURES'} or interval not in INTERVALS or not re.fullmatch(r'[A-Z0-9]{2,24}USDT',symbol):
        raise ValueError('Invalid chart venue, product, symbol or timeframe')
    with _lock:
        if http_get is not None:provider=PublicMarketData(exchange=exchange,http_get=http_get)
        else:
            if exchange not in _providers:_providers[exchange]=PublicMarketData(exchange=exchange)
            provider=_providers[exchange]
    if exchange=='bitget':
        gran={'1m':'1m','5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1Dutc'}[interval]
        if interval=='1d':
            path='/api/v2/spot/market/candles' if product=='SPOT' else '/api/v2/mix/market/candles'
            url=_url(BITGET,path,symbol=symbol,granularity=gran,limit=200,**({'productType':'USDT-FUTURES'} if product!='SPOT' else {}))
        else:url=_url(BITGET,'/api/v3/market/candles',category=product,symbol=symbol,interval=gran,limit=200,type='market')
    elif exchange=='binance':url=_url(BINANCE if product=='SPOT' else BINANCE_PERP,'/api/v3/klines' if product=='SPOT' else '/fapi/v1/klines',symbol=symbol,interval=interval,limit=200)
    else:url=_url(BYBIT,'/v5/market/kline',category='spot' if product=='SPOT' else 'linear',symbol=symbol,interval={'1m':'1','5m':'5','15m':'15','1h':'60','4h':'240','1d':'D'}[interval],limit=200)
    data,source,error=provider._get('chart_history',url,3)
    if error:return {'status':'ERROR','candles':[],'source':source,'error':error}
    rows=data if exchange=='binance' else data.get('data',[]) if exchange=='bitget' else data.get('result',{}).get('list',[])
    now=int(time.time()*1000);bars=normalize(rows,interval,now)
    return {'status':'FRESH_HISTORY' if bars else 'UNAVAILABLE','exchange':exchange,'symbol':symbol,'product':product,'interval':interval,'candles':bars,'fetched_at_ms':now,'source':source,'scope':'CHART_ONLY_NOT_ENTRY_CONFIRMATION'}
