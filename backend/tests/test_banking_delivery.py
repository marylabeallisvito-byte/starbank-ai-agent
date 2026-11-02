"""The built demo shell must keep API routes, assets and recovery boundaries intact."""
import tempfile
import unittest
from pathlib import Path
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from scripts.serve_banking import site_routes


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / 'data'
        self.root.mkdir(parents=True, exist_ok=True)

    def test_same_origin_demo_shell_assets_and_api(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = Path(tmp)
            (root / "index.html").write_text('<main>banking demo</main>', encoding='utf-8')
            (root / "assets").mkdir()
            (root / "assets/app.js").write_text('export {}', encoding='utf-8')
            async def health(request): return JSONResponse({"service": "banking"})
            app = Starlette(routes=[Route('/api/health', health), *site_routes(root)])
            client = TestClient(app)
            self.assertEqual(client.get('/banking').status_code, 200)
            self.assertEqual(client.get('/banking/').headers['cache-control'], 'no-store')
            self.assertEqual(client.get('/assets/app.js').status_code, 200)
            self.assertEqual(client.get('/api/health').json(), {"service": "banking"})
            self.assertEqual(client.get('/api/unknown').status_code, 404)
            self.assertEqual(client.get('/assets/missing.js').status_code, 404)

    def test_missing_build_is_actionable(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            with self.assertRaisesRegex(RuntimeError, 'npm run build'): site_routes(tmp)
