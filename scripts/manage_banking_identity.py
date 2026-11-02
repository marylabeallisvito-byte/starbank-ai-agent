"""Local administrator CLI. Credentials are written to a private file, never stdout."""
import argparse
import json
import os
import hashlib
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['create','disable','list','check'])
    parser.add_argument('--id')
    parser.add_argument('--name')
    parser.add_argument('--role',choices=['viewer','reviewer','auditor','admin'],default='viewer')
    parser.add_argument('--token-file')
    args=parser.parse_args()
    load_dotenv(ROOT/'.env',override=False)
    from backend import banking as b,banking_identity as identity
    with b._connect() as db:
        identity.schema(db)
        if args.action=='create':
            if not args.token_file or not args.id or not args.name:parser.error('create requires --id, --name and --token-file')
            path=Path(args.token_file).resolve()
            if not path.is_relative_to(ROOT/'backend/data/private'):parser.error('token file must stay inside backend/data/private')
            if path.exists():parser.error('credential file already exists; do not overwrite')
            path.parent.mkdir(parents=True,exist_ok=True)
            db.execute('BEGIN IMMEDIATE')
            token=identity.create(db,args.id,args.name,args.role)
            descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as output:output.write(token)
                db.commit()
            except Exception:
                db.rollback()
                path.unlink(missing_ok=True)
                raise
            print(json.dumps({'created':args.id,'role':args.role,'credential_file':str(path)}))
        elif args.action=='disable':
            if not args.id:parser.error('disable requires --id')
            changed=db.execute('UPDATE banking_operator_identities SET enabled=0 WHERE id=?',(args.id,)).rowcount
            print(json.dumps({'disabled':changed}))
        elif args.action=='check':
            credential=os.getenv('BANKING_OPERATOR_TOKEN','')
            row=db.execute("SELECT token_hash,enabled,role FROM banking_operator_identities WHERE id='local-reviewer'").fetchone()
            print(json.dumps({'configured':bool(credential),'format_valid':24<=len(credential)<=200,'identity_exists':bool(row),'credential_matches':bool(row and row['token_hash']==hashlib.sha256(credential.encode()).hexdigest()),'enabled':bool(row and row['enabled']),'role':row['role'] if row else None},ensure_ascii=False))
        else:
            print(json.dumps({'identities':[dict(row) for row in db.execute('SELECT id,display_name,role,enabled FROM banking_operator_identities ORDER BY id')]},ensure_ascii=False))


if __name__=='__main__':main()
