"""Single-origin, built frontend + FastAPI demo. Build frontend before starting.

python -m scripts.serve_banking --port 5173
Uses loopback only, no Vite development server or automatic source reload.
"""
import argparse
from pathlib import Path
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

DIST = Path(__file__).resolve().parents[1] / "frontend/dist"


def site_routes(directory):
    directory = Path(directory).resolve()
    index = directory / "index.html"
    if not index.is_file(): raise RuntimeError("前端尚未构建，请先在 frontend 目录执行 npm run build")

    async def shell(request):
        # Never cache entry HTML across releases; fingerprinted asset URLs are emitted by Vite.
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    return [Route("/banking/research", shell), Route("/banking/classic", shell), Route("/banking", shell), Route("/banking/", shell), Route("/", shell), Mount("/", app=StaticFiles(directory=directory))]


def create_app():
    from backend.main import app
    if not getattr(app.state, "built_site_installed", False):
        app.router.routes.extend(site_routes(DIST))
        app.state.built_site_installed = True
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5173)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535: parser.error("port must be between 1024 and 65535")
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")
