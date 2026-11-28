import { spawn, type ChildProcessByStdio } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';
import type { Readable } from 'node:stream';
import type { GlobalSetupContext } from 'vitest/node';

declare module 'vitest' {
  export interface ProvidedContext {
    isolatedApiOrigin: string;
    isolatedApiScratch: string;
  }
}

function waitForListeningOrigin(child: ChildProcessByStdio<null, null, Readable>) {
  return new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('隔离测试 API 未在 45 秒内启动。')), 45_000);
    const finish = () => clearTimeout(timeout);
    child.once('error', error => {
      finish();
      reject(new Error('无法启动隔离测试 API，请检查后端 Python 环境。', { cause: error }));
    });
    child.once('exit', (code, signal) => {
      finish();
      reject(new Error(`隔离测试 API 已退出，状态 ${code ?? signal}。`));
    });
    let pending = '';
    child.stderr.setEncoding('utf8').on('data', (chunk: string) => {
      pending = (pending + chunk).slice(-2048);
      const endpoint = /Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)\s/.exec(pending);
      if (endpoint) {
        finish();
        resolve(`${endpoint[1]}/api/v1`);
      }
    });
  });
}

export default async function setup({ provide }: GlobalSetupContext) {
  const backend = path.resolve(import.meta.dirname, '../../../backend');
  const python = path.join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const tempRoot = path.resolve(backend, '../.intermediate/tmp');
  await mkdir(tempRoot, { recursive: true });
  const scratch = await mkdtemp(path.join(tempRoot, 'frontend-real-api-'));
  const inheritedEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('SCENETURN_') && !key.startsWith('OPENFMV_')));
  const api = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '0', '--no-access-log'], {
    cwd: backend,
    env: {
      ...inheritedEnv,
      NODE_ENV: process.env.NODE_ENV,
      SCENETURN_DATA_DIR: path.join(scratch, 'data'),
      SCENETURN_TMP_DIR: path.join(scratch, 'media'),
      SCENETURN_WORKER_ENABLED: 'false',
      TMPDIR: scratch,
      XDG_CACHE_HOME: path.join(scratch, 'cache'),
      PYTHONDONTWRITEBYTECODE: '1',
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  const teardown = async () => {
    if (api.pid && api.exitCode === null && api.signalCode === null) {
      const exited = once(api, 'exit');
      const timeout = setTimeout(() => api.kill('SIGKILL'), 5_000);
      api.kill();
      await exited.finally(() => clearTimeout(timeout));
    }
    await rm(scratch, { recursive: true, force: true });
  };
  try {
    const origin = await waitForListeningOrigin(api);
    const response = await fetch(`${origin}/health`, { signal: AbortSignal.timeout(5_000) });
    if (!response.ok) throw new Error(`隔离测试 API 的健康检查返回 ${response.status}。`);
    provide('isolatedApiOrigin', origin);
    provide('isolatedApiScratch', scratch);
    return teardown;
  } catch (error) {
    await teardown();
    throw error;
  }
}
