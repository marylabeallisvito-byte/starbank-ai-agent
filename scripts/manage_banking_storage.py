"""Initialize or rotate local encryption keys without printing them.

Stop banking services before init/rotate. Previous keys are retained for old
backups; remove them only after separately verifying backup/recovery policy.
"""
import argparse
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import dotenv_values,load_dotenv,set_key

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['init','status','rotate'])
    parser.add_argument('--services-stopped',action='store_true')
    args=parser.parse_args()
    if args.action!='status' and not args.services_stopped:parser.error('stop banking services and supply --services-stopped')
    envfile=ROOT/'.env';values=dotenv_values(envfile)
    if args.action in {'init','rotate'}:
        active=values.get('BANKING_DATA_ENCRYPTION_KEY') or ''
        previous=values.get('BANKING_DATA_PREVIOUS_KEYS') or ''
        if active:Fernet(active.encode())
        if args.action=='rotate' or not active:
            if active:
                previous=','.join(dict.fromkeys([active,*[v for v in previous.split(',') if v]]))
                set_key(envfile,'BANKING_DATA_PREVIOUS_KEYS',previous)
            active=Fernet.generate_key().decode()
            set_key(envfile,'BANKING_DATA_ENCRYPTION_KEY',active)
        os.environ['BANKING_DATA_ENCRYPTION_KEY']=active
        os.environ['BANKING_DATA_PREVIOUS_KEYS']=previous
    load_dotenv(envfile,override=False)
    from backend import banking as b,banking_storage as storage
    with b._connect() as db:
        result=storage.status(db)
        result['database']=str(b.DB_PATH)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
