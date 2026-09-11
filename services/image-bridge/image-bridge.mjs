import http from "node:http";
import { spawn } from "node:child_process";
import { createReadStream, promises as fs } from "node:fs";
import path from "node:path";
import { pipeline } from "node:stream/promises";

const host = "127.0.0.1";
const port = Number(process.env.AI_CHAT_IMAGE_BRIDGE_PORT || 13003);
const token = process.env.AI_CHAT_IMAGE_BRIDGE_TOKEN || "";
const serviceHome = path.resolve(process.env.AI_CHAT_SERVICE_HOME || process.env.HOME || "/srv/ai-chat");
const imageAgent = process.env.AI_CHAT_IMAGE_AGENT || "image-generator";
const openclawBin = process.env.OPENCLAW_BIN || "openclaw";
const agentSessionsRoot = path.resolve(
  process.env.AI_CHAT_AGENT_SESSIONS_ROOT
    || path.join(serviceHome, ".openclaw", "agents", imageAgent, "sessions"),
);
const mediaRoot = path.resolve(
  process.env.AI_CHAT_IMAGE_ROOT
    || path.join(serviceHome, ".openclaw", "media", "tool-image-generation"),
);
const codexImageRoot = path.resolve(
  process.env.AI_CHAT_CODEX_IMAGE_ROOT
    || path.join(serviceHome, ".openclaw", "agents", imageAgent, "agent", "codex-home", "generated_images"),
);
const allowedImageRoots = [mediaRoot, codexImageRoot];
const maxRequestBytes = 16 * 1024;
const maxImageBytes = 30 * 1024 * 1024;

const SECOND_MS = 1_000;
const IMAGE_GENERATION_TIMEOUT_SECONDS = 15 * 60;
const AGENT_EXIT_GRACE_MS = 15 * SECOND_MS;
const AGENT_HARD_TIMEOUT_MS = IMAGE_GENERATION_TIMEOUT_SECONDS * SECOND_MS
  + AGENT_EXIT_GRACE_MS;
const IMAGE_DISCOVERY_TIMEOUT_MS = 3 * 60 * SECOND_MS;
const IMAGE_RESPONSE_TRANSFER_RESERVE_MS = 45 * SECOND_MS;
const WEBSITE_OUTER_TIMEOUT_MS = 20 * 60 * SECOND_MS;
const HTTP_REQUEST_TIMEOUT_MS = AGENT_HARD_TIMEOUT_MS
  + IMAGE_DISCOVERY_TIMEOUT_MS
  + IMAGE_RESPONSE_TRANSFER_RESERVE_MS;
const SHUTDOWN_FORCE_TIMEOUT_MS = HTTP_REQUEST_TIMEOUT_MS + 30 * SECOND_MS;

if (HTTP_REQUEST_TIMEOUT_MS >= WEBSITE_OUTER_TIMEOUT_MS) {
  throw new Error("图片桥超时必须短于网站外层超时");
}

let active = false;
let shuttingDown = false;
let shutdownForceTimer = null;
const activeControllers = new Set();

function cancellationError(signal) {
  if (signal?.reason instanceof Error) return signal.reason;
  const error = new Error("客户端已断开，图片任务已取消");
  error.code = "REQUEST_ABORTED";
  return error;
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw cancellationError(signal);
}

function abortableDelay(milliseconds, signal) {
  throwIfAborted(signal);
  if (!signal) return new Promise((resolve) => setTimeout(resolve, milliseconds));
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal.removeEventListener("abort", cancel);
      resolve();
    };
    const cancel = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      reject(cancellationError(signal));
    };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
}

function replyJson(response, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.length,
    "cache-control": "no-store",
  });
  response.end(body);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxRequestBytes) {
        reject(new Error("请求内容过大"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    request.on("error", reject);
  });
}

function runAgent(requestId, prompt, signal) {
  throwIfAborted(signal);
  const instruction = [
    "你是 AI Chat 的专用图片生成器。",
    "只允许使用 image_generate 工具，根据下方用户描述生成一张图片。",
    "不要调用浏览器、命令、文件、消息、网络搜索或其他工具，不要向任何聊天渠道发送内容。",
    "如果描述不是图片生成请求，直接拒绝。完成后只简短确认图片已生成。",
    "用户描述：",
    prompt,
  ].join("\n");
  const args = [
    "agent",
    "--agent", imageAgent,
    "--session-key", `agent:${imageAgent}:web-${requestId}`,
    "--model", "gpt-5.6-luna",
    "--thinking", "low",
    "--timeout", String(IMAGE_GENERATION_TIMEOUT_SECONDS),
    "--json",
    "--message", instruction,
  ];
  return new Promise((resolve, reject) => {
    const child = spawn(openclawBin, args, {
      env: {
        ...process.env,
        HOME: serviceHome,
        PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    let stdoutSize = 0;
    let stderrSize = 0;
    let terminationError = null;
    let settled = false;
    let timer = null;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    };
    const settle = (handler, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      handler(value);
    };
    const terminate = (error) => {
      if (terminationError || settled) return;
      terminationError = error;
      try {
        child.kill("SIGKILL");
      } catch (killError) {
        if (!terminationError.cause) terminationError.cause = killError;
      }
    };
    const onAbort = () => terminate(cancellationError(signal));
    timer = setTimeout(() => terminate(new Error("图片生成超时")), AGENT_HARD_TIMEOUT_MS);
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) onAbort();
    child.stdout.on("data", (chunk) => {
      stdoutSize += chunk.length;
      if (stdoutSize <= 2 * 1024 * 1024) stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderrSize += chunk.length;
      if (stderrSize <= 64 * 1024) stderr.push(chunk);
    });
    child.on("error", (error) => {
      if (child.pid && terminationError) return;
      settle(reject, terminationError || error);
    });
    child.on("close", (code) => {
      if (terminationError) {
        settle(reject, terminationError);
        return;
      }
      if (code !== 0) {
        const detail = Buffer.concat(stderr).toString("utf8").trim();
        settle(reject, new Error(detail || `图片任务退出，状态 ${code}`));
        return;
      }
      try {
        settle(resolve, JSON.parse(Buffer.concat(stdout).toString("utf8")));
      } catch {
        settle(reject, new Error("图片任务返回格式无效"));
      }
    });
  });
}

function cleanupSessionLater(sessionKey, attempt = 0) {
  const timer = setTimeout(async () => {
    try {
      const index = JSON.parse(await fs.readFile(path.join(agentSessionsRoot, "sessions.json"), "utf8"));
      const entry = index?.[sessionKey];
      if (!entry) return;
      if (entry.status !== "done") {
        if (attempt < 4) cleanupSessionLater(sessionKey, attempt + 1);
        return;
      }
      const sessionId = String(entry.sessionId || "");
      if (!/^[a-f0-9-]{36}$/.test(sessionId)) return;
      const transcript = path.resolve(String(entry.sessionFile || ""));
      if (!transcript.startsWith(`${agentSessionsRoot}${path.sep}`)) return;
      const artifacts = [
        transcript,
        path.join(agentSessionsRoot, `${sessionId}.trajectory.jsonl`),
        path.join(agentSessionsRoot, `${sessionId}.trajectory-path.json`),
      ];
      await Promise.all(artifacts.map((artifact) => fs.unlink(artifact).catch((error) => {
        if (error?.code !== "ENOENT") throw error;
      })));
      const child = spawn(openclawBin, [
        "sessions", "cleanup", "--agent", imageAgent, "--fix-missing", "--enforce", "--json",
      ], {
        env: {
          ...process.env,
          HOME: serviceHome,
          PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
        },
        stdio: "ignore",
      });
      child.on("error", (error) => console.error("Unable to clean image session index", error));
    } catch (error) {
      console.error("Unable to remove completed image session", error);
    }
  }, 60_000);
  timer.unref();
}

async function safeImage(candidate, startedAt) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate)) return null;
  let resolved;
  try {
    resolved = await fs.realpath(candidate);
  } catch {
    return null;
  }
  if (!allowedImageRoots.some((root) => resolved === root || resolved.startsWith(`${root}${path.sep}`))) return null;
  const stat = await fs.stat(resolved);
  if (!stat.isFile() || stat.size < 1 || stat.size > maxImageBytes) return null;
  if (stat.mtimeMs + 5_000 < startedAt) return null;
  const extension = path.extname(resolved).toLowerCase();
  const contentType = extension === ".jpg" || extension === ".jpeg" ? "image/jpeg"
    : extension === ".webp" ? "image/webp"
      : extension === ".png" ? "image/png"
        : "";
  if (!contentType) return null;
  const cleanup = setTimeout(() => {
    fs.unlink(resolved).catch((error) => {
      if (error?.code !== "ENOENT") console.error("Unable to remove intermediate image", error);
    });
  }, 30_000);
  cleanup.unref();
  return { path: resolved, size: stat.size, contentType };
}

async function recentCodexImages(startedAt, directory = codexImageRoot, depth = 0) {
  if (depth > 3) return [];
  let entries;
  try {
    entries = await fs.readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  const candidates = [];
  for (const entry of entries) {
    const candidate = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      candidates.push(...await recentCodexImages(startedAt, candidate, depth + 1));
      continue;
    }
    if (!entry.isFile() || !/\.(?:png|jpe?g|webp)$/i.test(entry.name)) continue;
    const stat = await fs.stat(candidate);
    if (stat.mtimeMs + 5_000 >= startedAt) candidates.push({ path: candidate, mtimeMs: stat.mtimeMs });
  }
  return candidates.sort((a, b) => b.mtimeMs - a.mtimeMs).map((item) => item.path);
}

async function completedSessionCandidates(sessionKey) {
  const index = JSON.parse(await fs.readFile(path.join(agentSessionsRoot, "sessions.json"), "utf8"));
  const sessionFile = index?.[sessionKey]?.sessionFile;
  if (typeof sessionFile !== "string" || !path.isAbsolute(sessionFile)) return [];
  const resolvedSession = await fs.realpath(sessionFile);
  if (!resolvedSession.startsWith(`${agentSessionsRoot}${path.sep}`)) return [];
  const lines = (await fs.readFile(resolvedSession, "utf8")).split("\n").filter(Boolean);
  const candidates = [];
  for (const line of lines) {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }
    const message = record?.message;
    if (
      message?.role !== "user"
      || message?.provenance?.kind !== "inter_session"
      || message?.provenance?.sourceTool !== "image_generate"
      || typeof message?.content !== "string"
      || !message.content.includes("status: completed successfully")
    ) continue;
    for (const match of message.content.matchAll(/path="([^"]+)"/g)) candidates.push(match[1]);
    for (const match of message.content.matchAll(/^MEDIA:(.+)$/gm)) candidates.push(match[1].trim());
  }
  return candidates;
}

async function generatedImage(result, sessionKey, startedAt, signal) {
  throwIfAborted(signal);
  const payloads = result?.result?.payloads;
  const candidates = (Array.isArray(payloads) ? payloads : []).flatMap((payload) => [
    ...(Array.isArray(payload?.mediaUrls) ? payload.mediaUrls : []),
    ...(typeof payload?.mediaUrl === "string" ? [payload.mediaUrl] : []),
  ]);
  for (const candidate of candidates) {
    throwIfAborted(signal);
    const image = await safeImage(candidate, startedAt);
    if (image) return image;
  }
  for (const candidate of await recentCodexImages(startedAt)) {
    throwIfAborted(signal);
    const image = await safeImage(candidate, startedAt);
    if (image) return image;
  }
  const deadline = Date.now() + IMAGE_DISCOVERY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    throwIfAborted(signal);
    try {
      for (const candidate of await completedSessionCandidates(sessionKey)) {
        throwIfAborted(signal);
        const image = await safeImage(candidate, startedAt);
        if (image) return image;
      }
      for (const candidate of await recentCodexImages(startedAt)) {
        throwIfAborted(signal);
        const image = await safeImage(candidate, startedAt);
        if (image) return image;
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await abortableDelay(Math.min(1_000, deadline - Date.now()), signal);
  }
  throw new Error("图片生成超时，请稍后重试");
}

const server = http.createServer(async (request, response) => {
  if (shuttingDown) {
    response.setHeader("connection", "close");
    replyJson(response, 503, { error: "图片生成服务正在停止，请稍后重试" });
    return;
  }
  if (request.method === "GET" && request.url === "/healthz") {
    replyJson(response, 200, { ok: true, busy: active });
    return;
  }
  if (request.method !== "POST" || request.url !== "/generate") {
    replyJson(response, 404, { error: "not found" });
    return;
  }
  if (!token || request.headers.authorization !== `Bearer ${token}`) {
    replyJson(response, 401, { error: "unauthorized" });
    return;
  }
  if (active) {
    replyJson(response, 429, { error: "图片生成器正在处理上一项任务，请稍后重试" });
    return;
  }
  active = true;
  const controller = new AbortController();
  activeControllers.add(controller);
  const cancelRequest = () => {
    if (controller.signal.aborted) return;
    const error = new Error("客户端已断开，图片任务已取消");
    error.code = "REQUEST_ABORTED";
    controller.abort(error);
  };
  const cancelOnResponseClose = () => {
    if (!response.writableFinished) cancelRequest();
  };
  request.once("aborted", cancelRequest);
  response.once("close", cancelOnResponseClose);
  try {
    const payload = JSON.parse(await readBody(request));
    const requestId = String(payload.requestId || "");
    const prompt = String(payload.prompt || "").trim();
    if (!/^[a-f0-9]{32}$/.test(requestId)) throw new Error("任务标识无效");
    if (!prompt || prompt.length > 6000) throw new Error("图片描述应为 1–6000 个字符");
    const startedAt = Date.now();
    const sessionKey = `agent:${imageAgent}:web-${requestId}`;
    const result = await runAgent(requestId, prompt, controller.signal);
    const image = await generatedImage(result, sessionKey, startedAt, controller.signal);
    cleanupSessionLater(sessionKey);
    response.writeHead(200, {
      "content-type": image.contentType,
      "content-length": image.size,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    });
    await pipeline(createReadStream(image.path), response, { signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted || response.destroyed) {
      if (!response.destroyed) response.destroy();
    } else if (response.headersSent) response.destroy(error);
    else replyJson(response, 502, { error: String(error?.message || "图片生成失败").slice(0, 500) });
  } finally {
    request.removeListener("aborted", cancelRequest);
    response.removeListener("close", cancelOnResponseClose);
    activeControllers.delete(controller);
    active = false;
  }
});

function beginGracefulShutdown(signalName) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`AI Chat image bridge received ${signalName}; waiting for active request to finish`);

  shutdownForceTimer = setTimeout(() => {
    const error = new Error("图片生成服务停止等待超时");
    error.code = "SHUTDOWN_TIMEOUT";
    for (const controller of activeControllers) {
      if (!controller.signal.aborted) controller.abort(error);
    }
    server.closeAllConnections?.();
    console.error("AI Chat image bridge forced shutdown after graceful timeout");
    process.exit(1);
  }, SHUTDOWN_FORCE_TIMEOUT_MS);

  server.close((error) => {
    if (shutdownForceTimer) clearTimeout(shutdownForceTimer);
    shutdownForceTimer = null;
    if (error) {
      console.error("AI Chat image bridge shutdown failed", error);
      process.exitCode = 1;
      return;
    }
    console.log("AI Chat image bridge stopped gracefully");
    process.exitCode = 0;
  });
  server.closeIdleConnections?.();
}

// Internal worst case: 900s generation + 15s process-exit grace + 180s
// artifact discovery. Keep another 45s for returning the image, while still
// finishing one minute before the website's 1,200s outer request timeout.
server.requestTimeout = HTTP_REQUEST_TIMEOUT_MS;
server.timeout = HTTP_REQUEST_TIMEOUT_MS;
server.headersTimeout = 10_000;
server.listen(port, host, () => {
  console.log(`AI Chat image bridge listening on http://${host}:${port}`);
});

process.once("SIGTERM", () => beginGracefulShutdown("SIGTERM"));
process.once("SIGINT", () => beginGracefulShutdown("SIGINT"));
