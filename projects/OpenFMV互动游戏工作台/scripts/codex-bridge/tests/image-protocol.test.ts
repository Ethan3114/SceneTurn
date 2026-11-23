import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFile, mkdir, readFile, symlink } from "node:fs/promises";
import { randomUUID, createHash } from "node:crypto";
import { resolve } from "node:path";
import { CodexProtocol } from "../src/protocol.js";
import { imageBytes, imageItemSchema } from "../src/image-files.js";
import { productRoot } from "../src/storage.js";
import { imageAccountAvailable } from "../src/image-runtime.js";


test("实际图片权限线程接受仅任务输入目录，危险协议仍拒绝，零模型回合", async () => {
  assert.equal(await imageAccountAvailable(), true);
  const cwd = resolve(productRoot, ".intermediate/codex-image-protocol-check", randomUUID());
  const inputsPath = resolve(cwd, "inputs");
  await mkdir(inputsPath, { recursive: true, mode: 0o700 });
  const protocol = new CodexProtocol(cwd, { inputsPath });
  try {
    await protocol.initialize();
    await protocol.assertChatgptAccount();
    assert.deepEqual(await protocol.assertRestrictedConfiguration(), { restricted: true, dynamicToolHostEnabled: true });
    const response = await protocol.request("thread/start", { cwd, permissions: "openfmv_image", runtimeWorkspaceRoots: [cwd], approvalPolicy: "never", dynamicTools: [] }) as {
      thread: { id: string }; activePermissionProfile: { id: string }; approvalPolicy: string;
    };
    assert.equal(response.activePermissionProfile.id, "openfmv_image");
    assert.equal(response.approvalPolicy, "never");
    assert.equal((await protocol.assertMcpIsolated(response.thread.id)).isolated, true);
    await assert.rejects(Promise.resolve(protocol.client.request("mcpServer/tool/call", { threadId: response.thread.id, server: "node_repl", tool: "js", arguments: {} })),
      (error: unknown) => error instanceof Error && "code" in error && error.code === -32603 && error.message === "unknown MCP server 'node_repl'");
    for (const method of ["thread/shellCommand", "command/exec", "process/spawn", "fs/readFile", "mcpServer/tool/call"]) {
      await assert.rejects(protocol.request(method, {}), /METHOD_NOT_AUTHORIZED/);
    }
    await assert.rejects(protocol.request("thread/turns/list", { threadId: response.thread.id, limit: 10 }),
      (error: unknown) => error instanceof Error && "code" in error && error.code === -32600 && /not materialized yet/.test(error.message));
  } finally { await protocol.close(); }
});

test("实际现有PNG文件的导入路径校验，不将文件引用当图片字节", async () => {
  const root = resolve(productRoot, ".intermediate/codex-image-files-check", randomUUID());
  const threadId = randomUUID();
  const itemId = randomUUID();
  const directory = resolve(root, "generated_images", threadId);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const source = resolve(productRoot, "frontend/public/logo.png");
  const destination = resolve(directory, `${itemId}.png`);
  await copyFile(source, destination);
  const input = imageItemSchema.parse({ type: "imageGeneration", id: itemId, status: "completed", savedPath: destination });
  const actual = await imageBytes(input, threadId, root);
  const original = await readFile(source);
  assert.deepEqual(actual.bytes, original);
  assert.equal(actual.sha256, createHash("sha256").update(original).digest("hex"));
  assert.equal(actual.source, "savedPath");
  await assert.rejects(imageBytes({ ...input, savedPath: source }, threadId, root), /CODEX_IMAGE_PATH_INVALID/);
  await assert.rejects(imageBytes({ type: "imageGeneration", id: itemId, status: "completed", fileId: "unresolved-file-reference" }, threadId, root), /CODEX_IMAGE_BYTES_UNAVAILABLE/);
  await assert.rejects(imageBytes({ ...input, status: "in_progress" }, threadId, root), /CODEX_IMAGE_NOT_COMPLETED/);
  const otherId = randomUUID();
  const link = resolve(directory, `${otherId}.png`);
  await symlink(source, link);
  await assert.rejects(imageBytes({ ...input, id: otherId, savedPath: link }, threadId, root), /CODEX_IMAGE_PATH_INVALID/);
  const inline = await imageBytes({ type: "imageGeneration", id: itemId, status: "completed", result: original.toString("base64") }, threadId, root);
  assert.equal(inline.sha256, actual.sha256);
  await assert.rejects(imageBytes({ type: "imageGeneration", id: itemId, status: "completed", result: "invalid!" }, threadId, root));
});
