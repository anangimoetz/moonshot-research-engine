import argparse
import json
import os
import subprocess
import sys
from .config import Settings
from .store import Store
from .pipeline import Scanner

def main():
    p=argparse.ArgumentParser(description="Public-market research, no trading")
    p.add_argument('--once',action='store_true');p.add_argument('--status',action='store_true')
    p.add_argument('--serve-local',action='store_true');p.add_argument('--authorize-rose',action='store_true')
    p.add_argument('--exchange',choices=['bitget','binance','bybit'],default='bitget')
    p.add_argument('--symbols',default='');p.add_argument('--limit',type=int,default=12)
    a=p.parse_args();cfg=Settings().validate()
    if a.authorize_rose:
        env=os.environ.copy();env['ROSE_ANALYSIS_ENGINE']='moonshot_scanner'
        env.setdefault('ROSE_STATE_DIR',str(cfg.state_dir/'rose'))
        return subprocess.call([sys.executable,'-m','rose_monitor.push_watch','--authorize'],env=env)
    store=Store(cfg.state_dir/'scanner.sqlite3')
    if a.status:print(json.dumps({'configuration':cfg.public_status(),'worker':store.get_state('worker',{}),'delivery':store.outbox_status()},indent=2));return 0
    if a.once:
        from .app import validate_scan
        request=validate_scan({'exchange':a.exchange,'symbols':a.symbols,'limit':a.limit})
        job_id=store.enqueue('scan',request)
        # Manual CLI does not steal other pending jobs.
        with store.connection() as db:db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?",(job_id,))
        try:
            result=Scanner(store).execute_scan({'id':job_id,'payload':request})
            store.finish(job_id,result);print(json.dumps({'job_id':job_id,**result},indent=2));return 0 if result['returned'] else 1
        except Exception as e:store.finish(job_id,error=type(e).__name__);print(type(e).__name__);return 1
    if a.serve_local:
        from .app import create_app
        create_app(start_worker=True).run(host='127.0.0.1',port=int(os.getenv('PORT','8000')),debug=False,use_reloader=False)
        return 0
    p.print_help();return 0

if __name__=='__main__':raise SystemExit(main())
