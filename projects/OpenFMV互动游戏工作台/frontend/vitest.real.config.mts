import { defineConfig } from 'vitest/config';
import path from 'node:path';

export default defineConfig({
  test: { environment: 'node', include: ['tests/real/**/*.test.ts', '__tests__/unit/nodeTimeline.test.ts'], testTimeout: 30000 },
  resolve: { alias: { '@': path.resolve(import.meta.dirname, '.') } },
});
