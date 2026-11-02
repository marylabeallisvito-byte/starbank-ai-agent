import json
from scripts.banking_delivery import build

if __name__=='__main__':print(json.dumps(build(),ensure_ascii=False))
