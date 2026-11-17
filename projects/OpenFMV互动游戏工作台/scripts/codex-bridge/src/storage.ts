import { mkdir, lstat, open, readFile, realpath, rename } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { findPackageJSON } from "node:module";
import { randomUUID } from "node:crypto";
import { z } from "zod";

const packagePath = findPackageJSON(import.meta.url);
if (!packagePath) throw new Error("BRIDGE_PACKAGE_NOT_FOUND");
export const productRoot = resolve(dirname(packagePath), "../..");
const intermediateRoot = resolve(productRoot, ".intermediate");
export const stateDirectory = resolve(productRoot, process.env.OPENFMV_BRIDGE_STATE_DIR ?? ".intermediate/codex-bridge");
const stateRelative = relative(intermediateRoot, stateDirectory);
if (!stateRelative || stateRelative.startsWith("..")) throw new Error("LOCAL_STATE_PATH_INVALID");

export const credentialSchema = z.object({
  apiBase: z.string().url(), connectionId: z.string(), projectId: z.string(), token: z.string().min(32),
  expiresAt: z.string().datetime({ offset: true }), version: z.string(),
}).strict();
export type Credentials = z.infer<typeof credentialSchema>;

export const checkpointSchema = z.object({
  runId: z.string(), threadId: z.string().nullable(), turnId: z.string().nullable(),
  revision: z.number().int().positive(), usage: z.record(z.string(), z.unknown()).nullable(),
  pendingCallId: z.string().nullable(),
  codexResumeOperationId: z.string().nullable(),
  threadUsageBaseline: z.record(z.string(), z.number().int().nonnegative()).nullable(),
  threadUsageTotal: z.record(z.string(), z.number().int().nonnegative()).nullable(),
  requestedReasoningEffort: z.literal("medium").optional(),
  toolCatalogVersion: z.enum(["R1", "R2", "R3", "R4"]).optional(),
}).strict();
export type Checkpoint = z.infer<typeof checkpointSchema>;

export async function prepareStateDirectory(): Promise<void> {
  const scope = await realpath(intermediateRoot);
  const parent = await realpath(dirname(stateDirectory));
  if (relative(productRoot, scope).startsWith("..") || relative(scope, parent).startsWith("..")) throw new Error("LOCAL_STATE_PATH_INVALID");
  await mkdir(stateDirectory, { recursive: true, mode: 0o700 });
  const actual = await realpath(stateDirectory);
  if (relative(scope, actual).startsWith("..")) throw new Error("LOCAL_STATE_PATH_INVALID");
  const info = await lstat(actual);
  if (info.mode & 0o077) throw new Error("LOCAL_STATE_PERMISSION_INVALID");
}

export async function readPrivateJson(path: string): Promise<unknown> {
  const info = await lstat(path);
  if (!info.isFile() || info.mode & 0o077) throw new Error("LOCAL_FILE_PERMISSION_INVALID");
  return JSON.parse(await readFile(path, "utf8")) as unknown;
}

export async function writePrivateJson(name: string, value: unknown): Promise<void> {
  await prepareStateDirectory();
  if (!/^[A-Za-z0-9_.-]+\.json$/.test(name)) throw new Error("LOCAL_STATE_NAME_INVALID");
  const target = resolve(stateDirectory, name);
  const pending = `${target}.${randomUUID()}.pending`;
  const file = await open(pending, "wx", 0o600);
  await file.writeFile(JSON.stringify(value));
  await file.sync();
  await file.close();
  await rename(pending, target);
}
