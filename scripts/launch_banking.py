"""Verify a release, then serve a single-origin loopback-only demo."""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv,dotenv_values
from scripts.banking_delivery import verify

ROOT=Path(__file__).resolve().parents[1]


def preflight(root=ROOT):
    result=verify(root);config=dotenv_values(Path(root)/'.env')
    if not config.get('BANKING_DATA_ENCRYPTION_KEY'):raise ValueError('Run python -m scripts.init_banking_demo or configure a valid local encryption key first')
    from cryptography.fernet import Fernet
    try:Fernet(config['BANKING_DATA_ENCRYPTION_KEY'].encode())
    except (ValueError,TypeError):raise ValueError('Local encryption key is invalid') from None
    if not 24<=len(config.get('BANKING_OPERATOR_TOKEN') or '')<=200:raise ValueError('Configure a valid workforce credential before the demo')
    if config.get('BANKING_LLM_ENABLED')=='1' and not config.get('LLM_API_KEY'):raise ValueError('Model enabled without a configured API key')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=5173);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('Invalid local port')
    result=preflight()
    if args.check:print(json.dumps(result))
    else:
        load_dotenv(ROOT/'.env',override=False)
        import uvicorn
        from scripts.serve_banking import create_app
        print('Open http://localhost:'+str(args.port)+'/banking')
        uvicorn.run(create_app(),host='127.0.0.1',port=args.port,log_level='warning')
