import { isIP } from 'node:net';

import type { NextRequest } from 'next/server';

const HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
]);
const REQUEST_PRIVATE_HEADERS = new Set([
  'host', 'content-length', 'expect', 'forwarded', 'x-real-ip',
  'x-client-ip', 'x-proxy-secret', 'x-envoy-external-address', 'x-cloudbase-context',
]);
const UPSTREAM_TIMEOUT_MS = 300_000;

type ProxyConfiguration = { backend: URL; secret?: string; cloudbaseEdge: boolean };

export function proxyConfiguration(environment: Readonly<Record<string, string | undefined>>): ProxyConfiguration | null {
  const deployment = environment.BACKEND_URL !== undefined;
  const localBackend = environment.SCENETURN_API_ORIGIN ?? environment.OPENFMV_API_ORIGIN;
  const raw = deployment ? environment.BACKEND_URL : localBackend || 'http://127.0.0.1:8310';
  let backend: URL;
  try {
    backend = new URL(raw ?? '');
  } catch {
    return null;
  }
  if (!['http:', 'https:'].includes(backend.protocol) || backend.username || backend.password || backend.search || backend.hash) return null;
  const secret = environment.PROXY_SHARED_SECRET;
  if (secret !== undefined && (!secret || secret !== secret.trim() || /[\r\n]/.test(secret))) return null;
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(backend.hostname);
  if ((deployment || !loopback) && !secret) return null;
  const trustedEdge = environment.SCENETURN_PROXY_TRUSTED_EDGE ?? environment.OPENFMV_PROXY_TRUSTED_EDGE;
  if (trustedEdge !== undefined && trustedEdge !== '' && trustedEdge !== 'cloudbase') return null;
  if (trustedEdge === 'cloudbase' && !secret) return null;
  return { backend, secret, cloudbaseEdge: trustedEdge === 'cloudbase' };
}

export function upstreamUrl(backend: URL, pathname: string, search: string): URL {
  if (pathname !== '/api/v1' && !pathname.startsWith('/api/v1/')) throw new Error('API_PATH_FORBIDDEN');
  const target = new URL(backend);
  target.pathname = backend.pathname.replace(/\/$/, '') + pathname;
  target.search = search;
  return target;
}

function connectionHeaders(headers: Headers): Set<string> {
  return new Set((headers.get('connection') ?? '').split(',').map(value => value.trim().toLowerCase()).filter(Boolean));
}

export function forwardHeaders(incoming: Headers, configuration: ProxyConfiguration): Headers {
  const headers = new Headers();
  const connection = connectionHeaders(incoming);
  incoming.forEach((value, key) => {
    const name = key.toLowerCase();
    if (HOP_HEADERS.has(name) || connection.has(name) || REQUEST_PRIVATE_HEADERS.has(name)
      || name.startsWith('x-forwarded-') || name.startsWith('x-middleware-') || name.startsWith('x-nextjs-')) return;
    headers.append(name, value);
  });
  headers.set('accept-encoding', 'identity');
  if (configuration.secret) headers.set('x-proxy-secret', configuration.secret);
  const address = incoming.get('x-envoy-external-address');
  if (configuration.cloudbaseEdge && address && isIP(address)) headers.set('x-client-ip', address);
  return headers;
}

export function responseHeaders(upstream: Headers, method: string, backend: URL, publicOrigin: string): Headers {
  const headers = new Headers();
  const connection = connectionHeaders(upstream);
  const compressed = upstream.has('content-encoding') && upstream.get('content-encoding') !== 'identity';
  upstream.forEach((value, key) => {
    const name = key.toLowerCase();
    if (HOP_HEADERS.has(name) || connection.has(name) || name === 'set-cookie'
      || name === 'x-proxy-secret' || name === 'x-client-ip' || name === 'x-envoy-external-address'
      || name === 'x-cloudbase-context'
      || (method !== 'HEAD' && compressed && ['content-length', 'content-encoding'].includes(name))) return;
    headers.append(name, value);
  });
  for (const cookie of upstream.getSetCookie()) headers.append('set-cookie', cookie);
  const location = headers.get('location');
  if (location) {
    const redirect = new URL(location, backend);
    if (redirect.origin === backend.origin) {
      const prefix = backend.pathname.replace(/\/$/, '');
      if (redirect.pathname === `${prefix}/api/v1` || redirect.pathname.startsWith(`${prefix}/api/v1/`)) {
        headers.set('location', publicOrigin + redirect.pathname.slice(prefix.length) + redirect.search + redirect.hash);
      }
    }
  }
  return headers;
}

function unavailable(status: number, code: string, message: string): Response {
  return Response.json({ error: { code, message } }, { status, headers: { 'cache-control': 'no-store' } });
}

export async function forwardApiRequest(request: NextRequest): Promise<Response> {
  const configuration = proxyConfiguration(process.env);
  if (!configuration || configuration.backend.origin === request.nextUrl.origin) {
    return unavailable(503, 'BACKEND_NOT_CONFIGURED', '服务连接尚未配置，请稍后再试');
  }
  const method = request.method.toUpperCase();
  const body = method === 'GET' || method === 'HEAD' ? undefined : request.body;
  const options: RequestInit & { duplex?: 'half' } = {
    method, headers: forwardHeaders(request.headers, configuration), body,
    ...(body ? { duplex: 'half' as const } : {}),
    redirect: 'manual', cache: 'no-store',
    signal: AbortSignal.any([request.signal, AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)]),
  };
  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl(configuration.backend, request.nextUrl.pathname, request.nextUrl.search), options);
  } catch (error) {
    const timedOut = error instanceof Error && error.name === 'TimeoutError';
    return unavailable(timedOut ? 504 : 502, timedOut ? 'BACKEND_TIMEOUT' : 'BACKEND_UNAVAILABLE',
      timedOut ? '服务响应超时，请查看原任务状态' : '服务暂时无法连接，请稍后再试');
  }
  const noBody = method === 'HEAD' || [204, 205, 304].includes(upstream.status);
  return new Response(noBody ? null : upstream.body, {
    status: upstream.status, statusText: upstream.statusText,
    headers: responseHeaders(upstream.headers, method, configuration.backend, request.nextUrl.origin),
  });
}
