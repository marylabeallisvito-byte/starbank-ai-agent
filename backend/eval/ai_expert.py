"""AI-simulated expert challenge; never presented as independent human evidence."""
import json
from pathlib import Path
from unittest.mock import patch
from backend.eval import monthly_challenge as runner


def run():
    here=Path(__file__).resolve().parent
    with patch.object(runner,'CORPUS',here/'ai_expert_v1.json'), patch.object(runner,'PROTOCOL',here/'ai_expert_protocol_v1.json'), patch.object(runner,'DIRECTORY',here/'reports/ai_expert_v1'):
        return runner.run(live_model=False)


if __name__=='__main__':
    result,path=run()
    print(json.dumps({'report':str(path),'passed':result['passed'],'total':result['total'],'unexpected_effects':result['unexpected_effects'],'cohort':'ai_simulated_expert','real_users':0}))
    raise SystemExit(0 if result['passed']==result['total'] else 1)
