"""Run registered regression modules without overwriting frozen reports."""
import ast,json,unittest
from pathlib import Path

def modules():
    path=Path(__file__).resolve().parents[1]/'backend/eval/banking_release_eval.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='loadTestsFromNames':
            return ast.literal_eval(node.args[0])
    raise RuntimeError('No registered regression modules found')

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromNames(modules())
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({'tests_run':result.testsRun,'passed':result.wasSuccessful(),'scope':'internal synthetic regression; no model or real users'}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
