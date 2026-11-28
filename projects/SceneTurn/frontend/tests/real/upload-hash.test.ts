import { createHash, randomBytes } from 'node:crypto';

import { expect, it } from 'vitest';

import { hashUploadFile } from '../../app/_utils/directUpload';

it('计算跨多个文件分块的完整 SHA-256', async () => {
  const bytes = Uint8Array.from(randomBytes(17 * 1024 * 1024 + 55));
  const file = new Blob([bytes]);
  await expect(hashUploadFile(file)).resolves.toBe(createHash('sha256').update(bytes).digest('hex'));
});
