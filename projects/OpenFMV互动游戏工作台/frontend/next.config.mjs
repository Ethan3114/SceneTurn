import createNextIntlPlugin from 'next-intl/plugin';

const withNextIntl = createNextIntlPlugin();

/** @type {import('next').NextConfig} */
const nextConfig = {
    output: 'standalone',
    allowedDevOrigins: ['127.0.0.1'],
    async rewrites() {
        return [{ source: '/api/v1/:path*', destination: 'http://127.0.0.1:8310/api/v1/:path*' }];
    },
    experimental: {
        // 允许1GiB文件及multipart边界，完整大小仍由后端校验。
        proxyClientMaxBodySize: 1025 * 1024 * 1024,
        proxyTimeout: 300000,
        serverActions: {
            bodySizeLimit: '10mb',
        },
    },
};

export default withNextIntl(nextConfig);
