import type { Metadata, Viewport } from 'next';
import { NextIntlClientProvider } from 'next-intl';
import './globals.css';
import 'tldraw/tldraw.css';

import enMessages from '@/messages/en.json';

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export const metadata: Metadata = {
  title: 'SceneTurn',
  description: '互动影像创作平台',
  icons: {
    icon: '/sceneturn/icon.svg',
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body>
        <NextIntlClientProvider locale="en" messages={enMessages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
