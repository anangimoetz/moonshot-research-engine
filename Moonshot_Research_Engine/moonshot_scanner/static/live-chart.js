/* Public chart feeds only. These updates never change research/entry decisions. */
(function(root){
'use strict';
const durations={'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000,'1d':86400000};
const bybitTF={'1m':'1','5m':'5','15m':'15','1h':'60','4h':'240','1d':'D'};
const bitgetTF={'1m':'1m','5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D'};
function protocol(c){
 if(!durations[c.interval]||!['SPOT','USDT-FUTURES'].includes(c.product)||!/^[A-Z0-9]{2,24}USDT$/.test(c.symbol))throw Error('Invalid chart instrument');
 if(c.exchange==='bitget')return {url:'wss://ws.bitget.com/v3/ws/public',subscription:{op:'subscribe',args:[{instType:c.product==='SPOT'?'spot':'usdt-futures',topic:'kline',symbol:c.symbol,interval:bitgetTF[c.interval]}]},restOnly:c.interval==='1d'};
 if(c.exchange==='binance')return {url:(c.product==='SPOT'?'wss://data-stream.binance.vision':'wss://fstream.binance.com')+'/ws/'+c.symbol.toLowerCase()+'@kline_'+c.interval};
 if(c.exchange==='bybit')return {url:'wss://stream.bybit.com/v5/public/'+(c.product==='SPOT'?'spot':'linear'),subscription:{op:'subscribe',args:['kline.'+bybitTF[c.interval]+'.'+c.symbol]}};
 throw Error('Unsupported exchange');
}
function bar(raw,c,now=Date.now()){
 const x={time:Number(raw.open_time)/1000,open:Number(raw.open),high:Number(raw.high),low:Number(raw.low),close:Number(raw.close)};
 if([raw.open_time,raw.open,raw.high,raw.low,raw.close].some(v=>v===null||v===undefined||typeof v==='boolean'||v===''))return null;
 if(!Object.values(x).every(Number.isFinite)||x.time*1000%durations[c.interval]||x.time*1000>now+5000||Math.min(x.open,x.high,x.low,x.close)<=0||!(x.low<=Math.min(x.open,x.close)&&Math.max(x.open,x.close)<=x.high))return null;
 x.is_closed=x.time*1000+durations[c.interval]<=now;
 return x;
}
function decode(message,c,now=Date.now()){
 if(!message||typeof message!=='object')return [];
 let rows=[],observed=Number(message.ts??message.E);
 if(c.exchange==='bitget'){
  const a=message.arg||{};
  if(a.symbol!==c.symbol||a.topic!=='kline'||a.interval!==bitgetTF[c.interval]||a.instType!==(c.product==='SPOT'?'spot':'usdt-futures'))return [];
  rows=(Array.isArray(message.data)?message.data:[]).filter(x=>x&&typeof x==='object').map(x=>({open_time:x.start,open:x.open,high:x.high,low:x.low,close:x.close}));
 }else if(c.exchange==='binance'){
  const k=message.k;if(message.s!==c.symbol||!k||k.i!==c.interval)return [];
  rows=[{open_time:k.t,open:k.o,high:k.h,low:k.l,close:k.c}];
 }else if(c.exchange==='bybit'){
  if(message.topic!=='kline.'+bybitTF[c.interval]+'.'+c.symbol)return [];
  rows=(Array.isArray(message.data)?message.data:[]).filter(x=>x&&typeof x==='object').map(x=>({open_time:x.start,open:x.open,high:x.high,low:x.low,close:x.close}));
 }
 if(!Number.isFinite(observed)||observed>now+5000)return [];
 return rows.map(x=>bar(x,c,now)).filter(Boolean).map(x=>({...x,observed_at_ms:observed}));
}
function merge(bars,next,interval){
 const old=bars.at(-1);if(old&&next.time<old.time)return {status:'OUT_OF_ORDER',bars};
 if(old&&next.time-old.time>durations[interval]/1000)return {status:'GAP',bars};
 const updated=bars.slice();if(old&&next.time===old.time)updated[updated.length-1]=next;else updated.push(next);
 return {status:'OK',bars:updated.slice(-300)};
}
function ema(bars,n){let value=null;return bars.filter(b=>b.is_closed).map((b,i)=>{value=value===null?b.close:b.close*2/(n+1)+value*(1-2/(n+1));return {time:b.time,value: i>=n-1?value:null};}).filter(x=>x.value!==null);}
class LiveChart{
 constructor(element,status,config,loadHistory){
  this.element=element;this.status=status;this.config={...config};this.loadHistory=loadHistory;this.bars=[];this.disposed=false;this.epoch=0;this.retry=0;this.lastEvent=null;
  const L=root.LightweightCharts;
  this.chart=L.createChart(element,{autoSize:true,height:330,layout:{background:{color:'#0b1320'},textColor:'#98abc5',attributionLogo:true},grid:{vertLines:{color:'#18263a'},horzLines:{color:'#18263a'}},timeScale:{timeVisible:true,secondsVisible:false},rightPriceScale:{borderColor:'#26344c'}});
  this.series=this.chart.addSeries(L.CandlestickSeries,{upColor:'#68d5ad',downColor:'#fa8a9a',wickUpColor:'#68d5ad',wickDownColor:'#fa8a9a',borderVisible:false});
  this.lines=[13,21].map((n,i)=>this.chart.addSeries(L.LineSeries,{color:i?'#bd9dff':'#71a5ff',lineWidth:1,lastValueVisible:false,priceLineVisible:false,title:'EMA'+n}));
  this.lastStatus='CONNECTING';this.timer=setInterval(()=>this.health(),5000);
  this.restart();
 }
 label(text){if(this.disposed)return;this.lastStatus=text;this.status.textContent=text;}
 closeSocket(){clearTimeout(this.reconnect);clearInterval(this.ping);if(this.socket){this.socket.onclose=null;this.socket.onerror=null;this.socket.close();this.socket=null;}}
 async restart(){
  if(this.disposed)return;const epoch=++this.epoch;this.closeSocket();this.lastEvent=null;this.label('LOADING HISTORY · '+this.config.exchange+' '+this.config.product+' '+this.config.interval);
  try{
   const d=await this.loadHistory(this.config);if(this.disposed||epoch!==this.epoch)return;
   this.bars=(d.candles||[]).map(x=>bar(x,this.config)).filter(Boolean).sort((a,b)=>a.time-b.time);
   this.bars=[...new Map(this.bars.map(x=>[x.time,x])).values()];if(!this.bars.length)throw Error('No chart history');
   this.series.applyOptions({priceFormat:{type:'price',precision:this.bars.at(-1).close<1?8:4,minMove:this.bars.at(-1).close<1?0.00000001:0.0001}});
   this.series.setData(this.bars.map(x=>this.paint(x)));this.indicators();this.levels();if(!this.started){this.chart.timeScale().fitContent();this.started=true;}
   const p=protocol(this.config);
   if(p.restOnly){this.label('REST UTC · refreshed '+new Date(d.fetched_at_ms).toISOString().slice(11,19)+' · stream daily non-UTC excluded');this.reconnect=setTimeout(()=>this.restart(),15000);return;}
   this.label('CONNECTING · history fetched '+new Date(d.fetched_at_ms).toISOString().slice(11,19)+' UTC');
   const ws=this.socket=new WebSocket(p.url);
   ws.onopen=()=>{if(this.disposed||epoch!==this.epoch)return;if(p.subscription)ws.send(JSON.stringify(p.subscription));this.label('CONNECTED · waiting for verified market update');this.ping=setInterval(()=>{if(ws.readyState===1&&this.config.exchange!=='binance')ws.send(this.config.exchange==='bitget'?'ping':JSON.stringify({op:'ping'}));},20000);};
   ws.onmessage=event=>{
    if(this.disposed||epoch!==this.epoch||event.data==='pong')return;
    let m;try{m=JSON.parse(event.data);}catch(_){return;}
    if(m.event==='error'||m.success===false){this.label('FEED ERROR · retrying');ws.close();return;}
    for(const b of decode(m,this.config)){
     if(Date.now()-b.observed_at_ms>90000){this.label('STALE · delayed exchange event');continue;}
     const r=merge(this.bars,b,this.config.interval);if(r.status==='OUT_OF_ORDER')continue;
     if(r.status==='GAP'){this.label('GAP · recovering REST history');this.restart();return;}
     this.bars=r.bars;this.series.update(this.paint(b));this.indicators();this.lastEvent=b.observed_at_ms;this.retry=0;
     this.label('LIVE · '+this.config.exchange+' '+this.config.product+' · '+b.close+' · event '+new Date(b.observed_at_ms).toISOString().slice(11,19)+' UTC · '+(b.is_closed?'CLOSED':'DEVELOPING'));
    }
   };
   ws.onerror=()=>this.label('CONNECTION ERROR · retrying');
   ws.onclose=()=>{if(this.disposed||epoch!==this.epoch)return;clearInterval(this.ping);this.label('RECONNECTING · last market event '+(this.lastEvent?new Date(this.lastEvent).toISOString():'UNKNOWN'));this.reconnect=setTimeout(()=>this.restart(),Math.min(30000,1000*2**Math.min(this.retry++,5)));};
  }catch(_){if(!this.disposed&&epoch===this.epoch){this.label('HISTORY UNAVAILABLE · retrying');this.reconnect=setTimeout(()=>this.restart(),10000);}}
 }
 paint(b){return {time:b.time,open:b.open,high:b.high,low:b.low,close:b.close,...(!b.is_closed?{color:'#e3bb68',wickColor:'#e3bb68'}:{})};}
 indicators(){this.lines.forEach((s,i)=>s.setData(ema(this.bars,i?21:13)));}
 levels(){if(this.levelsDrawn)return;this.levelsDrawn=true;for(const p of this.config.plans||[]){if(p.builder_status!=='CONDITIONAL')continue;for(const [key,color] of [['entry_mid','#71a5ff'],['stop','#fa8a9a'],['tp1','#68d5ad'],['tp2','#68d5ad']])if(typeof p[key]==='number'&&p[key]>0)this.series.createPriceLine({price:p[key],color,lineWidth:1,lineStyle:2,title:p.setup_type+' '+key});}}
 health(){if(this.disposed)return;if(this.lastEvent&&Date.now()-this.lastEvent>90000){this.label('STALE · no recent market updates; reconnecting');this.restart();}}
 destroy(){this.disposed=true;this.epoch++;this.closeSocket();clearInterval(this.timer);this.chart.remove();}
}
root.MoonshotChart={LiveChart,protocol,bar,decode,merge,ema,durations};
if(typeof module!=='undefined'&&module.exports)module.exports=root.MoonshotChart;
})(globalThis);
