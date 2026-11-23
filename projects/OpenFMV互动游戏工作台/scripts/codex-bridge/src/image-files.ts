import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { relative, resolve } from "node:path";
import { z } from "zod";


export const imageItemSchema = z.object({
  type: z.literal("imageGeneration"), id: z.string().regex(/^[A-Za-z0-9_-]{1,200}$/), status: z.string(),
  savedPath: z.string().nullable().optional(), result: z.string().optional(), fileId: z.string().optional(),
  inputImages: z.array(z.object({ path: z.string(), sha256: z.string().regex(/^[a-f0-9]{64}$/) })).optional(),
}).passthrough();
export type ImageItem = z.infer<typeof imageItemSchema>;
export const IMAGE_BYTES_LIMIT = 32 * 1024 * 1024;

export async function imageBytes(item: ImageItem, threadId: string, codexRoot = resolve(process.env.CODEX_HOME ?? resolve(homedir(), ".codex"))): Promise<{ bytes: Buffer; sha256: string; source: "savedPath" | "inline" }> {
  if (item.status !== "completed" || !/^[A-Za-z0-9_-]{1,200}$/.test(threadId)) throw new Error("CODEX_IMAGE_NOT_COMPLETED");
  let bytes: Buffer;
  let source: "savedPath" | "inline";
  if (item.savedPath) {
    const root = await realpath(codexRoot);
    const directory = resolve(root, "generated_images", threadId);
    const expected = resolve(directory, `${item.id}.png`);
    if (resolve(item.savedPath) !== expected) throw new Error("CODEX_IMAGE_PATH_INVALID");
    for (const path of [resolve(root, "generated_images"), directory, expected]) {
      if ((await lstat(path)).isSymbolicLink()) throw new Error("CODEX_IMAGE_PATH_INVALID");
    }
    const actual = await realpath(expected);
    if (actual !== expected || relative(root, actual).startsWith("..")) throw new Error("CODEX_IMAGE_PATH_INVALID");
    const file = await open(actual, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const info = await file.stat();
      if (!info.isFile() || info.nlink !== 1 || info.size <= 0 || info.size > IMAGE_BYTES_LIMIT) throw new Error("CODEX_IMAGE_FILE_INVALID");
      bytes = await file.readFile();
      const after = await file.stat();
      if (bytes.length !== info.size || after.size !== info.size || after.mtimeMs !== info.mtimeMs) throw new Error("CODEX_IMAGE_FILE_CHANGED");
    } finally { await file.close(); }
    source = "savedPath";
  } else if (item.result) {
    const encoded = z.string().max(Math.ceil(IMAGE_BYTES_LIMIT / 3) * 4).base64().parse(item.result.trim());
    bytes = Buffer.from(encoded, "base64");
    source = "inline";
  } else {
    throw new Error("CODEX_IMAGE_BYTES_UNAVAILABLE");
  }
  if (!bytes.length || bytes.length > IMAGE_BYTES_LIMIT) throw new Error("CODEX_IMAGE_FILE_INVALID");
  return { bytes, sha256: createHash("sha256").update(bytes).digest("hex"), source };
}
