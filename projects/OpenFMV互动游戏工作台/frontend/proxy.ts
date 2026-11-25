import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { hasLocale } from 'next-intl';
import createMiddleware from 'next-intl/middleware';

import { routing } from './i18n/routing';
import { forwardApiRequest } from './lib/proxy/forward';
import { publicPreviewEnabled, publicPreviewMessage } from './lib/public-preview';

const internationalize = createMiddleware(routing);
const previewPages = new Set(['', '/', '/privacy', '/terms']);
const previewAssets = new Set([
  '/sceneturn/icon.svg',
  '/sceneturn/tide.jpg',
  '/sceneturn/signal.jpg',
  '/sceneturn/port.jpg',
  '/sceneturn/home/ai.webp',
  '/sceneturn/home/codex.webp',
  '/sceneturn/home/blueprint.webp',
  '/sceneturn/home/timeline.webp',
  '/sceneturn/home/player.webp',
  '/sceneturn/home/assets.webp',
]);

function previewProxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const locale = pathname.split('/', 2)[1];
  const path = hasLocale(routing.locales, locale) ? pathname.slice(locale.length + 1) : pathname;
  const message = publicPreviewMessage(locale);
  const headers = { 'cache-control': 'no-store' };

  if (path === '/api' || path.startsWith('/api/')) {
    return NextResponse.json({ error: { code: 'PUBLIC_PREVIEW_UNAVAILABLE', message } }, { status: 404, headers });
  }

  const page = previewPages.has(path);
  const asset = previewAssets.has(pathname) || pathname.startsWith('/_next/static/');
  if (!page && !asset) {
    return new NextResponse(message, { status: 404, headers: { ...headers, 'content-type': 'text/plain; charset=utf-8' } });
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    return new NextResponse(message, { status: 405, headers: { ...headers, allow: 'GET, HEAD', 'content-type': 'text/plain; charset=utf-8' } });
  }
  return page ? internationalize(request) : NextResponse.next();
}

export default function proxy(request: NextRequest) {
  if (publicPreviewEnabled()) return previewProxy(request);

  const { pathname } = request.nextUrl;
  if (pathname === '/api/v1' || pathname.startsWith('/api/v1/')) {
    return forwardApiRequest(request);
  }
  if (/^\/(?:api|_next|_vercel)|\./.test(pathname)) return NextResponse.next();
  return internationalize(request);
}

