"""Export aggregate pilot metrics only; never participant tokens or comments."""
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--protocol',choices=['usability-pilot-v1','monthly-usability-v2'],default='usability-pilot-v1');args=parser.parse_args()
    load_dotenv(ROOT/'.env',override=False)
    from backend.banking_research import report
    result=report(args.protocol)
    result['exported_at']=datetime.now(timezone.utc).isoformat()
    directory=ROOT/'output/research';directory.mkdir(parents=True,exist_ok=True)
    path=directory/('pilot_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'file':str(path),'completed_external_sessions':result['completed_external_sessions'],'metrics_available':result['metrics'] is not None}))


if __name__=='__main__':main()
