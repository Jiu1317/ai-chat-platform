import assert from "node:assert/strict";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

const bridgePath = fileURLToPath(new URL("./image-bridge.mjs", import.meta.url));
const projectRoot = path.resolve(path.dirname(bridgePath), "..", "..");
const token = "test-token";
const onePixelPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

async function unusedPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitUntil(check, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch (error) {
      lastError = error;
    }
    await delay(25);
  }
  throw lastError || new Error("condition was not met before timeout");
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

function requestJson(port, pathname) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: "127.0.0.1", port, path: pathname }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        try {
          resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks)) });
        } catch (error) {
          reject(error);
        }
      });
    });
    request.once("error", reject);
  });
}

function generateRequest(port) {
  const body = Buffer.from(JSON.stringify({
    requestId: "0123456789abcdef0123456789abcdef",
    prompt: "生成测试图片",
  }));
  return http.request({
    host: "127.0.0.1",
    port,
    path: "/generate",
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "content-length": body.length,
    },
  });
}

function collectResponse(request) {
  return new Promise((resolve, reject) => {
    request.once("response", (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        contentType: response.headers["content-type"],
        body: Buffer.concat(chunks),
      }));
      response.once("error", reject);
    });
    request.once("error", reject);
  });
}

async function startBridge(t, mode) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "image-bridge-test-"));
  const port = await unusedPort();
  const fakeAgentPath = path.join(directory, "agent");
  const fakePidPath = path.join(directory, "agent.pid");
  const imageRoot = path.join(directory, "images");
  const imagePath = path.join(imageRoot, "generated.png");
  const agentSource = [
    'const fs = require("node:fs");',
    'const path = require("node:path");',
    'fs.writeFileSync(process.env.FAKE_AGENT_PID_PATH, String(process.pid));',
    'if (process.env.FAKE_AGENT_MODE === "success") {',
    '  const image = Buffer.from(process.env.FAKE_IMAGE_BASE64, "base64");',
    '  fs.mkdirSync(path.dirname(process.env.FAKE_IMAGE_PATH), { recursive: true });',
    '  fs.writeFileSync(process.env.FAKE_IMAGE_PATH, image);',
    '  process.stdout.write(JSON.stringify({ result: { payloads: [{ mediaUrls: [process.env.FAKE_IMAGE_PATH] }] } }));',
    '} else if (process.env.FAKE_AGENT_MODE === "slow-success") {',
    '  setTimeout(() => {',
    '    const image = Buffer.from(process.env.FAKE_IMAGE_BASE64, "base64");',
    '    fs.mkdirSync(path.dirname(process.env.FAKE_IMAGE_PATH), { recursive: true });',
    '    fs.writeFileSync(process.env.FAKE_IMAGE_PATH, image);',
    '    process.stdout.write(JSON.stringify({ result: { payloads: [{ mediaUrls: [process.env.FAKE_IMAGE_PATH] }] } }));',
    '  }, 750);',
    '} else if (process.env.FAKE_AGENT_MODE === "missing") {',
    '  process.stdout.write(JSON.stringify({ result: { payloads: [] } }));',
    '} else {',
    '  setInterval(() => {}, 1_000);',
    '}',
  ].join("\n");
  await fs.writeFile(fakeAgentPath, agentSource, "utf8");

  const stdout = [];
  const stderr = [];
  const bridge = spawn(process.execPath, [bridgePath], {
    cwd: directory,
    env: {
      ...process.env,
      AI_CHAT_IMAGE_BRIDGE_PORT: String(port),
      AI_CHAT_IMAGE_BRIDGE_TOKEN: token,
      AI_CHAT_SERVICE_HOME: directory,
      AI_CHAT_IMAGE_ROOT: imageRoot,
      AI_CHAT_CODEX_IMAGE_ROOT: path.join(directory, "codex-images"),
      AI_CHAT_AGENT_SESSIONS_ROOT: path.join(directory, "sessions"),
      OPENCLAW_BIN: process.execPath,
      FAKE_AGENT_MODE: mode,
      FAKE_AGENT_PID_PATH: fakePidPath,
      FAKE_IMAGE_PATH: imagePath,
      FAKE_IMAGE_BASE64: onePixelPng.toString("base64"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  bridge.stdout.on("data", (chunk) => stdout.push(chunk));
  bridge.stderr.on("data", (chunk) => stderr.push(chunk));

  t.after(async () => {
    try {
      const fakePid = Number(await fs.readFile(fakePidPath, "utf8"));
      if (fakePid && isAlive(fakePid)) process.kill(fakePid, "SIGKILL");
    } catch {}
    if (bridge.exitCode === null && bridge.signalCode === null) {
      const closed = once(bridge, "close");
      bridge.kill("SIGKILL");
      await closed;
    }
    await fs.rm(directory, { recursive: true, force: true });
  });

  await waitUntil(async () => {
    if (bridge.exitCode !== null) {
      throw new Error(`bridge exited early: ${Buffer.concat(stderr).toString("utf8")}`);
    }
    return Buffer.concat(stdout).toString("utf8").includes("AI Chat image bridge listening");
  });
  return { port, fakePidPath, bridge, stdout, stderr };
}

test("client disconnect terminates the agent and releases capacity", async (t) => {
  const fixture = await startBridge(t, "hang");
  const request = generateRequest(fixture.port);
  request.on("error", () => {});
  request.end(JSON.stringify({
    requestId: "0123456789abcdef0123456789abcdef",
    prompt: "生成测试图片",
  }));

  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === true);
  await waitUntil(async () => {
    try {
      return Boolean(await fs.readFile(fixture.fakePidPath, "utf8"));
    } catch {
      return false;
    }
  });
  const fakePid = Number(await fs.readFile(fixture.fakePidPath, "utf8"));

  request.destroy();

  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === false);
  await waitUntil(() => !isAlive(fakePid));
  assert.equal(isAlive(fakePid), false);
});

test("normal image response completes without cancellation", async (t) => {
  const fixture = await startBridge(t, "success");
  const request = generateRequest(fixture.port);
  const responsePromise = collectResponse(request);
  request.end(JSON.stringify({
    requestId: "0123456789abcdef0123456789abcdef",
    prompt: "生成测试图片",
  }));
  const response = await responsePromise;

  assert.equal(response.status, 200);
  assert.equal(response.contentType, "image/png");
  assert.deepEqual(response.body, onePixelPng);
  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === false);
});

test("client disconnect interrupts image discovery polling", async (t) => {
  const fixture = await startBridge(t, "missing");
  const request = generateRequest(fixture.port);
  request.on("error", () => {});
  request.end(JSON.stringify({
    requestId: "0123456789abcdef0123456789abcdef",
    prompt: "生成测试图片",
  }));

  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === true);
  await waitUntil(async () => {
    try {
      const fakePid = Number(await fs.readFile(fixture.fakePidPath, "utf8"));
      return fakePid > 0 && !isAlive(fakePid);
    } catch {
      return false;
    }
  });

  request.destroy();

  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === false);
});

test("SIGTERM drains the active image response and refuses new connections", {
  skip: process.platform === "win32" ? "Windows does not deliver graceful SIGTERM to child processes" : false,
}, async (t) => {
  const fixture = await startBridge(t, "slow-success");
  const request = generateRequest(fixture.port);
  const responsePromise = collectResponse(request);
  request.end(JSON.stringify({
    requestId: "0123456789abcdef0123456789abcdef",
    prompt: "生成测试图片",
  }));

  await waitUntil(async () => (await requestJson(fixture.port, "/healthz")).body.busy === true);
  const bridgeClosePromise = once(fixture.bridge, "close");
  assert.equal(fixture.bridge.kill("SIGTERM"), true);
  await waitUntil(async () => {
    try {
      await requestJson(fixture.port, "/healthz");
      return false;
    } catch (error) {
      return ["ECONNREFUSED", "ECONNRESET"].includes(error?.code);
    }
  });
  assert.equal(fixture.bridge.exitCode, null, "bridge exited before its active request completed");

  const response = await responsePromise;
  assert.equal(response.status, 200);
  assert.equal(response.contentType, "image/png");
  assert.deepEqual(response.body, onePixelPng);
  await bridgeClosePromise;
  assert.equal(fixture.bridge.exitCode, 0, Buffer.concat(fixture.stderr).toString("utf8"));
  assert.match(Buffer.concat(fixture.stdout).toString("utf8"), /stopped gracefully/);
});

test("systemd and the updater preserve the bridge graceful-stop window", async () => {
  const unit = await fs.readFile(
    path.join(projectRoot, "deploy", "systemd", "ai-chat-image-bridge.service"),
    "utf8",
  );
  assert.match(unit, /^TimeoutStopSec=20min$/m);

  const update = await fs.readFile(path.join(projectRoot, "scripts", "update-server.sh"), "utf8");
  const installUnit = update.indexOf(
    'install -m 0644 "${project_root}/deploy/systemd/ai-chat-image-bridge.service"',
  );
  const daemonReload = update.indexOf("systemctl daemon-reload", installUnit);
  const stopWebsite = update.indexOf("systemctl stop ai-chat.service", daemonReload);
  const restartBridge = update.indexOf("systemctl restart ai-chat-image-bridge.service", stopWebsite);
  assert.ok(installUnit >= 0);
  assert.ok(installUnit < daemonReload);
  assert.ok(daemonReload < stopWebsite);
  assert.ok(daemonReload < restartBridge);
  assert.match(update, /systemctl start ai-chat-image-bridge\.service \|\| true/);
});
