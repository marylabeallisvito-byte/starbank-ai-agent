"""Initialise a fresh local demo; never overwrites an existing environment."""
import json
import os
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]


def initialise(root=ROOT):
    root=Path(root).resolve();env=root/'.env'
    if env.exists():return {'created':False,'reason':'existing_configuration_preserved'}
    private=root/'backend/data/private';private.mkdir(parents=True,exist_ok=True)
    credential=private/'local-reviewer.token';token=secrets.token_urlsafe(36)
    descriptor=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as output:output.write(token)
    try:
        descriptor=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as output:
            output.write('APP_ENV=development\nBANKING_SANDBOX_ENABLED=1\nBANKING_AUTH_MODE=passkey\nBANKING_LLM_ENABLED=0\nBANKING_DATA_ENCRYPTION_KEY='+Fernet.generate_key().decode()+'\nBANKING_OPERATOR_TOKEN='+token+'\n')
    except Exception:
        credential.unlink(missing_ok=True);raise
    return {'created':True,'credential_file':'backend/data/private/local-reviewer.token','model_enabled':False,'funds_environment':'synthetic'}


if __name__=='__main__':print(json.dumps(initialise(),ensure_ascii=False))
