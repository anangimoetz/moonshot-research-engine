"""One-host production supervisor. No source is declared connected by configuration."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from .config import Settings
from .pipeline import timestamp
from .store import Store

def preflight(env):
    if not env.get('SCANNER_TOKEN'):raise ValueError('Set SCANNER_TOKEN privately')
    if env.get('SCANNER_ROSE_ENABLED','0')=='1':
        required=['ROSE_TG_API_ID','ROSE_TG_API_HASH','ROSE_BOT_TOKEN','ROSE_CHAT_ID']
        if not all(env.get(k) for k in required):raise ValueError('Rose source and recipient credentials are incomplete')
        if not (Path(env['ROSE_STATE_DIR'])/'rose-source.session').is_file():raise ValueError('Authorize Rose locally before unattended startup')

def main():
    cfg=Settings().validate();env=os.environ.copy()
    env.setdefault('ROSE_STATE_DIR',str(cfg.state_dir/'rose'));env['ROSE_ANALYSIS_ENGINE']='moonshot_scanner'
    env.setdefault('ROSE_BOT_TOKEN',cfg.bot_token)
    env.setdefault('ROSE_CHAT_ID',cfg.chat_id)
    try:preflight(env)
    except ValueError as e:print(str(e),file=sys.stderr);return 2
    port=env.get('PORT','8000')
    if not port.isdigit() or not 1<=int(port)<=65535:return 2
    commands={'web':[sys.executable,'-m','gunicorn','--workers','1','--threads','8','--timeout','120','--graceful-timeout','25','--bind','0.0.0.0:'+port,'moonshot_scanner.app:create_app(start_worker=True)']}
    if env.get('SCANNER_ROSE_ENABLED','0')=='1':commands['rose']=[sys.executable,'-m','rose_monitor.push_watch']
    stopping=False;children={};attempts={};started={};retry_at={};history=[];store=Store(cfg.state_dir/'scanner.sqlite3')
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            for name,cmd in commands.items():
                child=children.get(name)
                if child and child.poll() is None:
                    if name=='web' and time.monotonic()-started[name]>120:
                        state=store.get_state('worker',{});beat=timestamp(state.get('heartbeat_at'))
                        if state.get('status')=='FAILED' or beat is None or time.time()*1000-beat>90000:child.terminate()
                    continue
                now=time.monotonic();history=[t for t in history if now-t<3600]
                if child:
                    history.append(now);attempts[name]=attempts.get(name,0)+1
                    if len(history)>10:print('Repeated process failure; inspect logs',file=sys.stderr);return 1
                    retry_at[name]=now+min(60,2**min(attempts[name],6))
                    children.pop(name,None)
                if now<retry_at.get(name,0):continue
                children[name]=subprocess.Popen(cmd,env=env);started[name]=now
            time.sleep(1)
    finally:
        for child in children.values():
            if child.poll() is None:child.terminate()
        for child in children.values():
            try:child.wait(timeout=30)
            except subprocess.TimeoutExpired:child.kill();child.wait()
    return 0
if __name__=='__main__':raise SystemExit(main())
