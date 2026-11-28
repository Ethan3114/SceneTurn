import { randomBytes } from 'node:crypto';

import { describe, expect, it } from 'vitest';

import { forwardHeaders, proxyConfiguration, responseHeaders, upstreamUrl } from '../../lib/proxy/forward';

describe('同源API运行时配置与来源约束', () => {
  it('保留本地默认与显式本地端口，部署配置缺少共享密钥时停止转发', () => {
    expect(proxyConfiguration({})?.backend.origin).toBe('http://127.0.0.1:8310');
    expect(proxyConfiguration({ SCENETURN_API_ORIGIN: 'http://127.0.0.1:19831' })?.backend.origin).toBe('http://127.0.0.1:19831');
    expect(proxyConfiguration({ BACKEND_URL: 'http://127.0.0.1:19831' })).toBeNull();
    expect(proxyConfiguration({ BACKEND_URL: '' })).toBeNull();
    expect(proxyConfiguration({ SCENETURN_API_ORIGIN: 'https://backend.example' })).toBeNull();
  });

  it('固定服务端目标，拒绝网址凭据和查询配置，保留API路径与查询串', () => {
    const secret = randomBytes(32).toString('base64url');
    const configuration = proxyConfiguration({ BACKEND_URL: 'https://backend.example/service/', PROXY_SHARED_SECRET: secret });
    expect(configuration).not.toBeNull();
    const target = upstreamUrl(configuration!.backend, '/api/v1/projects', '?url=https%3A%2F%2Fother.example');
    expect(target.origin).toBe('https://backend.example');
    expect(target.pathname).toBe('/service/api/v1/projects');
    expect(target.searchParams.get('url')).toBe('https://other.example');
    expect(proxyConfiguration({ BACKEND_URL: 'file:///private', PROXY_SHARED_SECRET: secret })).toBeNull();
    expect(proxyConfiguration({ BACKEND_URL: 'https://user:password@backend.example', PROXY_SHARED_SECRET: secret })).toBeNull();
    expect(proxyConfiguration({ BACKEND_URL: 'https://backend.example/?host=other.example', PROXY_SHARED_SECRET: secret })).toBeNull();
    expect(() => upstreamUrl(configuration!.backend, '//other.example', '')).toThrow('API_PATH_FORBIDDEN');
  });

  it('覆盖浏览器来源头，Cookie、Authorization、Range和multipart保持原值', () => {
    const secret = randomBytes(32).toString('base64url');
    const incoming = new Headers({ Cookie: 'author-selected=value', Authorization: 'author-selected-token', Range: 'bytes=0-15',
      'content-type': 'multipart/form-data; boundary=author-boundary', 'content-length': '100', 'connection': 'keep-alive, x-hop-value',
      'x-hop-value': 'hop', 'x-client-ip': '192.0.2.1', 'x-proxy-secret': 'browser-selected', 'x-envoy-external-address': '192.0.2.2',
      'x-forwarded-for': '192.0.2.3', 'x-forwarded-host': 'other.example', 'x-cloudbase-context': 'platform-context' });
    const configuration = proxyConfiguration({ BACKEND_URL: 'https://backend.example', PROXY_SHARED_SECRET: secret })!;
    const headers = forwardHeaders(incoming, configuration);
    expect(headers.get('x-proxy-secret') === secret).toBe(true);
    expect(headers.has('x-client-ip')).toBe(false);
    for (const key of ['x-forwarded-for', 'x-forwarded-host', 'x-envoy-external-address', 'content-length', 'connection', 'x-hop-value', 'x-cloudbase-context']) {
      expect(headers.has(key)).toBe(false);
    }
    for (const key of ['cookie', 'authorization', 'range', 'content-type']) expect(headers.get(key)).toBe(incoming.get(key));
    const trusted = { ...configuration, cloudbaseEdge: true };
    expect(forwardHeaders(incoming, trusted).get('x-client-ip')).toBe('192.0.2.2');
    incoming.set('x-envoy-external-address', '192.0.2.2, 192.0.2.3');
    expect(forwardHeaders(incoming, trusted).has('x-client-ip')).toBe(false);
  });

  it('服务端平台上下文不会随响应进入浏览器', () => {
    const upstream = new Headers({
      'x-cloudbase-context': 'platform-context',
      'content-range': 'bytes 0-15/100',
      'set-cookie': 'scene=public; HttpOnly',
    });
    const headers = responseHeaders(upstream, 'GET', new URL('https://backend.example'), 'https://sceneturn.example');
    expect(headers.has('x-cloudbase-context')).toBe(false);
    expect(headers.get('content-range')).toBe('bytes 0-15/100');
    expect(headers.getSetCookie()).toEqual(['scene=public; HttpOnly']);
  });
});
