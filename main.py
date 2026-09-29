import os,asyncio,hmac,hashlib,json,time,logging
from pathlib import Path
from urllib.parse import parse_qsl
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application,CommandHandler
load_dotenv()
BASE=Path(__file__).resolve().parent
WEB=BASE/'web'

TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
KEY=os.getenv('MARKET_DATA_API_KEY','').strip()
NEWS=os.getenv('NEWS_API_KEY','').strip()

SYMBOL=os.getenv('SYMBOL','XAU/USD')
INTERVAL=os.getenv('INTERVAL','5min')
PORT=int(os.getenv('PORT','8080'))
AUTO=os.getenv('AUTO_MONITOR','true').lower() in ('1','true','yes','on')
CHECK=max(15,int(os.getenv('CHECK_INTERVAL_SECONDS','60'))) SYMBOL=os.getenv('SYMBOL','XAU/USD'); INTERVAL=os.getenv('INTERVAL','5min'); PORT=int(os.getenv('PORT','8080')); AUTO=os.getenv('AUTO_MONITOR','true').lower() in ('1','true','yes','on'); CHECK=max(15,int(os.getenv('CHECK_INTERVAL_SECONDS','60')))
subs=set(); history=[]; last_key=''; bot_app=None; monitor_task=None; log=logging.getLogger('xau'); logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
async def req(url,params):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get(url,params=params) as r:return r.status,await r.json(content_type=None)
async def price():
    st,d=await req('https://api.twelvedata.com/price',{'symbol':SYMBOL,'apikey':KEY});
    if st!=200 or 'price' not in d:raise RuntimeError(str(d))
    return float(d['price'])
async def candles():
    st,d=await req('https://api.twelvedata.com/time_series',{'symbol':SYMBOL,'interval':INTERVAL,'outputsize':100,'apikey':KEY})
    if st!=200 or not d.get('values'):raise RuntimeError(str(d))
    return [{'datetime':x['datetime'],'open':float(x['open']),'high':float(x['high']),'low':float(x['low']),'close':float(x['close'])} for x in reversed(d['values'])]
def ema(v,p):
    o=[None]*len(v)
    if len(v)<p:return o
    a=sum(v[:p])/p;o[p-1]=a;k=2/(p+1)
    for i in range(p,len(v)):a=(v[i]-a)*k+a;o[i]=a
    return o
def rsi(v,p=14):
    o=[None]*len(v)
    if len(v)<=p:return o
    g=[];l=[]
    for i in range(1,len(v)):d=v[i]-v[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g[:p])/p;al=sum(l[:p])/p;o[p]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(p,len(g)):ag=(ag*(p-1)+g[i])/p;al=(al*(p-1)+l[i])/p;o[i+1]=100 if al==0 else 100-100/(1+ag/al)
    return o
def atr(c,p=14):
    tr=[]
    for i,x in enumerate(c):
        tr.append(x['high']-x['low'] if i==0 else max(x['high']-x['low'],abs(x['high']-c[i-1]['close']),abs(x['low']-c[i-1]['close'])))
    o=[None]*len(c)
    if len(tr)<p:return o
    a=sum(tr[:p])/p;o[p-1]=a
    for i in range(p,len(tr)):a=(a*(p-1)+tr[i])/p;o[i]=a
    return o
def analyze(c):
    if len(c)<60:return {'signal':'NO TRADE','reason':'Need 60 candles.'}
    v=[x['close'] for x in c];f=ema(v,9);s=ema(v,21);r=rsi(v);a=atr(c);i=len(c)-1
    if any(x is None for x in (f[i],s[i],r[i],a[i])):return {'signal':'NO TRADE','reason':'Indicators not ready.'}
    p=v[i];buy=p>f[i]>s[i] and 50<=r[i]<=70;sell=p<f[i]<s[i] and 30<=r[i]<=50
    if buy:sg,reason='BUY','Bullish EMA structure with RSI confirmation.';entry=p;sl=p-a[i]*1.5;tp1=p+a[i]*1.5;tp2=p+a[i]*3
    elif sell:sg,reason='SELL','Bearish EMA structure with RSI confirmation.';entry=p;sl=p+a[i]*1.5;tp1=p-a[i]*1.5;tp2=p-a[i]*3
    else:sg,reason='NO TRADE','No confirmed EMA + RSI setup.';entry=sl=tp1=tp2=None
    return {'signal':sg,'reason':reason,'time':c[i]['datetime'],'price':p,'ema_fast':f[i],'ema_slow':s[i],'rsi':r[i],'atr':a[i],'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2}
async def headlines():
    if not NEWS:return []
    st,d=await req('https://newsapi.org/v2/everything',{'q':'gold OR XAU OR USD OR Federal Reserve OR inflation','language':'en','sortBy':'publishedAt','pageSize':8,'apiKey':NEWS});
    return [{'title':a.get('title',''),'url':a.get('url','')} for a in d.get('articles',[]) if a.get('title')] if st==200 else []
def user_from(request):
    raw=request.headers.get('X-Telegram-Init-Data','')
    if not raw:return None
    try:
        p=dict(parse_qsl(raw,keep_blank_values=True));received=p.pop('hash',None);check='\n'.join(f'{k}={p[k]}' for k in sorted(p));secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest();calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not received or not hmac.compare_digest(calc,received):return None
        if int(p.get('auth_date','0')) and time.time()-int(p['auth_date'])>86400:return None
        return json.loads(p['user']) if p.get('user') else {}
    except Exception:return None
async def api_user(r):
    u=user_from(r)
    return web.json_response({'ok':True,'user':u}) if u is not None else web.json_response({'ok':False,'error':'Invalid Telegram init data'},status=401)
async def api_market(r):
    try:return web.json_response({'ok':True,'symbol':SYMBOL,'price':await price()})
    except Exception as e:return web.json_response({'ok':False,'error':str(e)},status=502)
async def api_signal(r):
    try:
        x=analyze(await candles());history.append(x);del history[:-30];return web.json_response({'ok':True,'data':x})
    except Exception as e:return web.json_response({'ok':False,'error':str(e)},status=502)
async def api_history(r):return web.json_response({'ok':True,'items':history[-20:][::-1]})
async def api_news(r):return web.json_response({'ok':True,'items':await headlines()})
async def monitor():
    global last_key
    while True:
        try:
            x=analyze(await candles());k=f"{x.get('signal')}|{x.get('time')}|{x.get('price')}"
            if x.get('signal') in ('BUY','SELL') and k!=last_key:
                last_key=k;history.append(x);del history[:-30]
                msg=f"🚨 XAUUSD AUTO SIGNAL\n\n{x['signal']}\nPrice: {x.get('price',0):.2f}\nEntry: {x.get('entry',0):.2f}\nSL: {x.get('sl',0):.2f}\nTP1: {x.get('tp1',0):.2f}\nTP2: {x.get('tp2',0):.2f}\n\n⚠️ Algorithmic alert only."
                for cid in list(subs):
                    try:await bot_app.bot.send_message(chat_id=cid,text=msg)
                    except Exception:pass
        except asyncio.CancelledError:raise
        except Exception as e:log.warning('monitor: %s',e)
        await asyncio.sleep(CHECK)
async def start(app):
    global monitor_task
    if AUTO:monitor_task=asyncio.create_task(monitor())
async def stop(app):
    global monitor_task
    if monitor_task:monitor_task.cancel()
async def cmd_start(u,c):
    if u.effective_chat:subs.add(u.effective_chat.id)
    await u.message.reply_text('🟡 XAUUSD Mini App Bot\n\nOpen the Mini App from Telegram.\n/signal - analyze\n/market - price\n/news - headlines\n/chatid - chat ID')
async def cmd_signal(u,c):
    try:await u.message.reply_text(json.dumps(analyze(await candles()),indent=2))
    except Exception as e:await u.message.reply_text(f'Signal error: {e}')
async def cmd_market(u,c):
    try:await u.message.reply_text(f'XAU/USD: {await price():.2f}')
    except Exception as e:await u.message.reply_text(f'Market error: {e}')
async def cmd_news(u,c):
    x=await headlines();await u.message.reply_text('\n'.join(f'{i+1}. {a["title"]}' for i,a in enumerate(x)) if x else 'No news available.')
async def cmd_chatid(u,c):await u.message.reply_text(str(u.effective_chat.id))
async def main():
    global bot_app
    if not TOKEN or not KEY:raise RuntimeError('Set TELEGRAM_BOT_TOKEN and MARKET_DATA_API_KEY in .env')
    bot_app=Application.builder().token(TOKEN).post_init(start).post_shutdown(stop).build()
    for cmd,fn in [('start',cmd_start),('signal',cmd_signal),('market',cmd_market),('news',cmd_news),('chatid',cmd_chatid)]:bot_app.add_handler(CommandHandler(cmd,fn))
    webapp=web.Application();webapp.router.add_get('/',lambda r:web.FileResponse(WEB/'index.html'));webapp.router.add_static('/static/',WEB);webapp.router.add_get('/api/user',api_user);webapp.router.add_get('/api/market',api_market);webapp.router.add_get('/api/signal',api_signal);webapp.router.add_get('/api/history',api_history);webapp.router.add_get('/api/news',api_news)
    runner=web.AppRunner(webapp);await runner.setup();await web.TCPSite(runner,'0.0.0.0',PORT).start();await bot_app.initialize();await bot_app.start();await bot_app.updater.start_polling();await asyncio.Event().wait()
if __name__=='__main__':asyncio.run(main())
