"""Disposable subprocess fault injector for encrypted synthetic test databases only."""
import json
import os
import sys
from pathlib import Path


def main():
    request=json.loads(sys.stdin.read())
    path=Path(os.environ['BANKING_SANDBOX_DB']).resolve()
    root=Path(__file__).resolve().parents[1]/'data'
    if path.name!='test.sqlite3' or not path.is_relative_to(root) or path.parent==root:
        raise RuntimeError('Only isolated test.sqlite3 databases are accepted')
    from backend import banking_missions as missions
    original=missions.event
    def crash_before_commit(db,row,name,detail):
        original(db,row,name,detail)
        if (request['operation']=='reserve' and name=='NODE_CHECKPOINT' and detail.get('node_id')=='reserve') or (request['operation']=='release' and name=='MISSION_BUDGET_COMPENSATED'):
            os._exit(71)
    if request['phase']=='before_commit':missions.event=crash_before_commit
    if request['operation']=='reserve':missions.tick(request['mission_id'])
    else:missions.action(request['mission_id'],missions.MissionAction(action='release'),request['customer_token'])
    os._exit(72)


if __name__=='__main__':main()
