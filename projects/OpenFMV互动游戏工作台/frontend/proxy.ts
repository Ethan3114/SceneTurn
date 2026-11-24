import createMiddleware from 'next-intl/middleware';
import type { NextRequest } from 'next/server';

import { routing } from './i18n/routing';
import { forwardApiRequest } from './lib/proxy/forward';

const internationalize = createMiddleware(routing);

export default function proxy(request: NextRequest) {
  if (request.nextUrl.pathname === '/api/v1' || request.nextUrl.pathname.startsWith('/api/v1/')) {
    return forwardApiRequest(request);
  }
  return internationalize(request);
}

export const config = {
  matcher: ['/api/v1/:path*', '/((?!api|_next|_vercel|.*\\..*).*)'],
};

