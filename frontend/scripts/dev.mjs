import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const projectRoot = path.resolve(frontendRoot, '..');
const python = path.join(projectRoot, '.venv', 'Scripts', 'python.exe');
const viteEntry = path.join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js');
const children = [];

function start(command, args, cwd) {
  const child = spawn(command, args, { cwd, stdio: 'inherit', windowsHide: true });
  children.push(child);
  return child;
}

if (existsSync(python)) {
  const backend = start(
    python,
    ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', '8000', '--reload', '--reload-dir', 'backend'],
    projectRoot,
  );
  backend.on('error', (error) => {
    console.error(`[dev] FastAPI 启动失败，银行业务不可用：${error.message}`);
  });
  backend.on('exit', (code) => {
    if (code && code !== 0) console.error(`[dev] FastAPI 已退出（code ${code}），请恢复后端后再办理银行业务。`);
  });
} else {
  console.error('[dev] 未找到 .venv\\Scripts\\python.exe；银行业务不可用，请先配置 Python 环境。');
}

const vite = start(process.execPath, [viteEntry, '--host', '127.0.0.1', '--port', '5173'], frontendRoot);
vite.on('exit', (code) => process.exit(code ?? 0));

function shutdown() {
  for (const child of children) {
    if (!child.killed) child.kill();
  }
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('exit', shutdown);
