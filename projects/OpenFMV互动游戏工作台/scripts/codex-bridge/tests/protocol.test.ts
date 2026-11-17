import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { execFileSync, spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { CodexProtocol, installedVersion, SUPPORTED_VERSION } from "../src/protocol.js";
import { productRoot } from "../src/storage.js";

test("真实Codex初始化与账户状态读取，未授权方法没有启动子进程", async () => {
  const cwd = resolve(productRoot, ".intermediate/codex-protocol-check");
  await mkdir(cwd, { recursive: true, mode: 0o700 });
  const protocol = new CodexProtocol(cwd);
  try {
    await protocol.initialize();
    assert.deepEqual(await protocol.assertRestrictedConfiguration(), { restricted: true, dynamicToolHostEnabled: true });
    assert.equal(installedVersion(), SUPPORTED_VERSION);
    const account = await protocol.request("account/read", { refreshToken: false });
    assert.equal(typeof account, "object");
    const childrenBefore = spawnSync("pgrep", ["-P", String(protocol.process.pid)], { encoding: "utf8" });
    assert.ok(childrenBefore.status === 0 || childrenBefore.status === 1);
    for (const method of ["thread/shellCommand", "command/exec", "process/spawn"]) {
      await assert.rejects(protocol.request(method, { command: "exit 99" }), (error: unknown) => error instanceof Error && error.message === "METHOD_NOT_AUTHORIZED");
    }
    const childrenAfter = spawnSync("pgrep", ["-P", String(protocol.process.pid)], { encoding: "utf8" });
    assert.equal(childrenAfter.stdout, childrenBefore.stdout);
    assert.ok(protocol.process.pid);
    assert.equal(process.kill(protocol.process.pid!, 0), true);
  } finally { await protocol.close(); }
});

test("真实app-server接受只读线程及本机动态工具Schema，未启动模型回合", async () => {
  const cwd = resolve(productRoot, ".intermediate/codex-protocol-check");
  await mkdir(cwd, { recursive: true, mode: 0o700 });
  const protocol = new CodexProtocol(cwd);
  try {
    await protocol.initialize();
    const catalog = JSON.parse(execFileSync(resolve(productRoot, "backend/.venv/bin/python"), ["-c", "import json; from app.tools.project_tools import tool_catalog; print(json.dumps(tool_catalog('writer', 'R2')))"], {
      cwd: resolve(productRoot, "backend"), encoding: "utf8",
    })) as Array<{ name: string; description: string; inputSchema: unknown }>;
    assert.ok(catalog.some((tool) => tool.name === "read_artifact"));
    assert.ok(catalog.some((tool) => tool.name === "patch_artifact"));
    const response = await protocol.request("thread/start", {
      cwd, sandbox: "read-only", approvalPolicy: "never", serviceName: "openfmv-protocol-check",
      dynamicTools: catalog.map((tool) => ({ type: "function", ...tool })),
    }) as { thread: { id: string }; sandbox: { type: string }; approvalPolicy: string };
    assert.equal(typeof response.thread.id, "string");
    assert.equal(response.sandbox.type, "readOnly");
    assert.equal(response.approvalPolicy, "never");
    const isolation = await protocol.assertMcpIsolated(response.thread.id);
    assert.equal(isolation.isolated, true);
    await assert.rejects(Promise.resolve(protocol.client.request("mcpServer/tool/call", { threadId: response.thread.id, server: "node_repl", tool: "js", arguments: { code: "", title: "检查本机工具隔离" } })),
      (error: unknown) => error instanceof Error && "code" in error && error.code === -32603 && error.message === "unknown MCP server 'node_repl'");
    await assert.rejects(protocol.request("mcpServer/tool/call", {}), (error: unknown) => error instanceof Error && error.message === "METHOD_NOT_AUTHORIZED");
  } finally { await protocol.close(); }
});

test("实际终止本机连接进程后可启动新进程并恢复认证状态", async () => {
  const cwd = resolve(productRoot, ".intermediate/codex-protocol-check");
  const original = new CodexProtocol(cwd);
  await original.initialize();
  const previousPid = original.process.pid;
  const disconnected = new Promise<void>((resolve) => original.once("closed", () => resolve()));
  original.process.kill("SIGTERM");
  await disconnected;
  await original.close();
  const restarted = new CodexProtocol(cwd);
  try {
    await restarted.initialize();
    assert.notEqual(restarted.process.pid, previousPid);
    const account = await restarted.request("account/read", { refreshToken: false });
    assert.equal(typeof account, "object");
  } finally { await restarted.close(); }
});
