import { spawn, type ChildProcess } from "node:child_process";
import { lstat, mkdir } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

import { installedVersion } from "./protocol.js";
import { prepareStateDirectory, stateDirectory } from "./storage.js";

const origin = "https://sceneturn.ethan-g.online";
const host = "127.0.0.1";
const port = 3212;
const pairingSchema = z.object({
  pairingId: z.uuid(), pairingCode: z.string().regex(/^[A-Za-z0-9_-]{32,200}$/), projectId: z.uuid(),
}).strict();
const root = resolve(stateDirectory, "online");
const command = fileURLToPath(new URL("./cli.ts", import.meta.url));

async function readPairing(request: IncomingMessage) {
  let length = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    length += chunk.length;
    if (length > 1024) throw new Error("PAIRING_BODY_TOO_LARGE");
    chunks.push(chunk);
  }
  return pairingSchema.parse(JSON.parse(Buffer.concat(chunks).toString("utf8")));
}

function respond(response: ServerResponse, status: number) {
  response.writeHead(status, { "Cache-Control": "no-store", "Content-Length": "0" }).end();
}

export async function serveDesktopPairing(): Promise<void> {
  installedVersion();
  await prepareStateDirectory();
  await mkdir(root, { mode: 0o700, recursive: true });
  const directory = await lstat(root);
  if (!directory.isDirectory() || directory.mode & 0o077) throw new Error("LOCAL_STATE_PERMISSION_INVALID");

  const workers = new Map<string, ChildProcess>();
  const server = createServer({ headersTimeout: 5000, requestTimeout: 5000 }, (request, response) => {
    if (request.headers.origin !== origin || request.headers.host !== `${host}:${port}`) {
      respond(response, 403);
      return;
    }
    response.setHeader("Access-Control-Allow-Origin", origin);
    response.setHeader("Vary", "Origin");
    if (request.url !== "/pair") {
      respond(response, 404);
      return;
    }
    if (request.method === "OPTIONS" && request.headers["access-control-request-method"] === "POST") {
      response.setHeader("Access-Control-Allow-Methods", "POST");
      response.setHeader("Access-Control-Allow-Headers", "Content-Type");
      respond(response, 204);
      return;
    }
    if (request.method !== "POST" || request.headers["content-type"] !== "application/json") {
      respond(response, 415);
      return;
    }
    void readPairing(request).then((pairing) => {
      if (!workers.has(pairing.projectId)) {
        const child = spawn(process.execPath, ["--import", "tsx", command, "pair", "--request-stdin", "--run"], {
          cwd: fileURLToPath(new URL("../", import.meta.url)),
          env: { ...process.env, SCENETURN_BRIDGE_STATE_DIR: resolve(root, pairing.projectId) },
          stdio: ["pipe", "ignore", "ignore"],
        });
        workers.set(pairing.projectId, child);
        child.once("close", () => workers.delete(pairing.projectId));
        child.once("error", () => workers.delete(pairing.projectId));
        child.stdin.on("error", () => child.kill());
        child.stdin.end(JSON.stringify({ ...pairing, apiBase: `${origin}/api/v1` }));
      }
      respond(response, 202);
    }, () => respond(response, 400));
  });
  const shutdown = () => {
    server.close();
    for (const child of workers.values()) child.kill("SIGTERM");
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
  await new Promise<void>((done, reject) => {
    server.once("close", done);
    server.once("error", reject);
    server.listen(port, host);
  });
}
