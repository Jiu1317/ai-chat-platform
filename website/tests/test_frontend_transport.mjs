import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const sources = [
  ["public", path.resolve(testDirectory, "../static/app.js")],
  ["runtime", path.resolve(testDirectory, "../../../../static/app.js")],
];

function functionSource(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `missing function ${name}`);
  const start = match.index;
  const tail = source.slice(start + 1);
  const next = /\n(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(/.exec(tail);
  return source.slice(start, next ? start + 1 + next.index : source.length);
}

function constantSource(source, name) {
  const start = source.indexOf(`const ${name} = `);
  assert.ok(start >= 0, `missing constant ${name}`);
  const end = source.indexOf(";\n", start);
  assert.ok(end > start, `unterminated constant ${name}`);
  return source.slice(start, end + 1);
}

function loadHelpers(source) {
  const context = vm.createContext({
    AbortController,
    DOMException,
    clearTimeout,
    setTimeout,
  });
  const names = [
    "stripInternalAnnotations",
    "userFacingError",
    "abortError",
    "waitForUploadRetry",
    "canRetryUpload",
    "uploadStep",
  ];
  vm.runInContext(names.map((name) => functionSource(source, name)).join("\n"), context);
  return context;
}

for (const [label, sourcePath] of sources) {
  const source = fs.readFileSync(sourcePath, "utf8");
  const htmlPath = label === "public"
    ? path.resolve(testDirectory, "../templates/index.html")
    : path.resolve(testDirectory, "../../../../templates/index.html");
  const html = fs.readFileSync(htmlPath, "utf8");

  test(`${label}: internal citation markers are removed`, () => {
    const helpers = loadHelpers(source);
    const citation = "\uE200cite\uE202turn123view4\uE201";
    assert.equal(helpers.stripInternalAnnotations(`前文${citation}后文`), "前文后文");
    assert.equal(helpers.stripInternalAnnotations(`前文\uE200cite\uE202turn123`, { removeIncomplete: true }), "前文");
  });

  test(`${label}: known transfer failures have readable messages`, () => {
    const helpers = loadHelpers(source);
    assert.match(helpers.userFacingError("unauthorized"), /重新登录/);
    assert.match(helpers.userFacingError("Image download failed after 3 attempts"), /下载转发失败/);
    assert.match(helpers.userFacingError("Send not acknowledged"), /未确认消息发送/);
    assert.match(helpers.userFacingError("gateway", 524), /524/);
    assert.match(helpers.userFacingError("Failed to fetch"), /网络连接中断/);
  });

  test(`${label}: upload retry stops immediately when cancelled`, async () => {
    const helpers = loadHelpers(source);
    const controller = new AbortController();
    let attempts = 0;
    const pending = helpers.uploadStep(() => {
      attempts += 1;
      const error = new Error("temporary");
      error.status = 503;
      throw error;
    }, controller.signal);
    setTimeout(() => controller.abort(), 10);
    await assert.rejects(pending, (error) => error?.name === "AbortError");
    assert.equal(attempts, 1);
  });

  test(`${label}: turn and upload fetches receive abort signals`, () => {
    assert.match(source, /signal:\s*turnRequest\.signal/);
    assert.match(source, /uploadFileInChunks\([\s\S]*?controller\?\.signal/);
    assert.match(source, /activeTurnRequest\?\.abort\(\)/);
    assert.match(source, /uploadController\?\.abort\(\)/);
    assert.match(source, /activeProjectUploadController\?\.abort\(\)/);
  });

  test(`${label}: the same file can be selected again after an early rejection`, () => {
    assert.match(
      source,
      /const selectedFiles = \[\.\.\.elements\.fileInput\.files\];\s*elements\.fileInput\.value = "";\s*uploadSelectedFiles\(selectedFiles\)/,
    );
  });

  test(`${label}: dropping a file outside the composer cannot navigate away`, () => {
    assert.match(source, /window\.addEventListener\("drop", \(event\) => \{\s*if \(hasDraggedFiles\(event\)\) event\.preventDefault\(\)/);
  });

  test(`${label}: 30 MB and five-file limits agree with the interface`, () => {
    assert.match(source, /const MAX_UPLOAD_BYTES = 30 \* MEBIBYTE/);
    assert.match(source, /const MAX_TURN_FILES = 5/);
    assert.match(source, /selectedTotal > MAX_UPLOAD_BYTES/);
    assert.match(source, /originalTextBytes\) \+ currentFileBytes > MAX_UPLOAD_BYTES/);
    assert.match(html, /最多 5 个，单个不超过 30 MB；文字与附件合计不超过 30 MB/);
  });

  test(`${label}: only no-attachment direct image requests enter image recovery`, () => {
    const context = vm.createContext({});
    vm.runInContext(`
      ${constantSource(source, "IMAGE_REQUEST_PATTERN")}
      ${constantSource(source, "IMAGE_DISCUSSION_PATTERN")}
      ${functionSource(source, "isDirectImageRequest")}
    `, context);
    assert.equal(context.isDirectImageRequest("请帮我生成一张小猫图片"), true);
    assert.equal(context.isDirectImageRequest("为什么不能生成图片"), false);
    assert.equal(context.isDirectImageRequest("请帮我生成一张小猫图片", [{ id: "reference" }]), false);
    assert.equal(context.isDirectImageRequest("请帮我生成一张小猫图片", [], [{ id: "project-reference" }]), false);
  });
}

const publicSource = fs.readFileSync(sources[0][1], "utf8");
const publicHtml = fs.readFileSync(
  path.resolve(testDirectory, "../templates/index.html"),
  "utf8",
);

test("public: assistant image files remain visible outside dedicated image mode", () => {
  assert.match(publicSource, /const isImage = isImageFile/);
  assert.doesNotMatch(publicSource, /if \(isImageFile && message\.mode !== "image"\) continue/);
});

test("public: rapid stream deltas are rendered at most once per animation frame", () => {
  const body = { innerHTML: "" };
  const article = {
    dataset: { messageId: "message-1" },
    classList: { toggle() {} },
    querySelector() { return body; },
  };
  const callbacks = [];
  const stats = { renders: 0, scrolls: 0 };
  const context = vm.createContext({
    callbacks,
    globalElements: {
      messageList: {
        querySelector(selector) {
          return selector.includes(article.dataset.messageId) ? article : null;
        },
      },
    },
    requestAnimationFrame(callback) { callbacks.push(callback); },
    stats,
    window: { scrollTo() { stats.scrolls += 1; } },
    document: { documentElement: { scrollHeight: 0 } },
  });
  vm.runInContext(`
    let pendingStreamingMessage = null;
    let streamingRenderScheduled = false;
    let autoScrollEnabled = true;
    const elements = globalElements;
    function renderAssistantMessageBody(body, message) {
      stats.renders += 1;
      body.innerHTML = String(message.content || "");
    }
    ${functionSource(publicSource, "updateStreamingMessage")}
  `, context);

  context.updateStreamingMessage({ id: "message-1", content: "第一段" });
  context.updateStreamingMessage({ id: "message-1", content: "第二段" });
  assert.equal(callbacks.length, 1);
  callbacks.shift()();
  assert.equal(stats.renders, 1);
  assert.equal(stats.scrolls, 1);
  assert.equal(callbacks.length, 0);
  assert.equal(body.innerHTML, "第二段");
});

test("public: stream chunks are appended without rescanning the whole answer", () => {
  const apply = functionSource(publicSource, "applyTurnEvent");
  assert.match(apply, /assistantMessage\.content \+= String\(event\.text \|\| ""\)/);
  assert.doesNotMatch(
    apply,
    /stripInternalAnnotations\(assistantMessage\.content \+ String\(event\.text/,
  );
});

test("public: both upload pickers expose the supported GIF and AVIF formats", () => {
  const accepts = [...publicHtml.matchAll(/<input[^>]+(?:file-input|project-file-input)[^>]+accept="([^"]+)"/g)]
    .map((match) => match[1]);
  assert.equal(accepts.length, 2);
  accepts.forEach((accept) => {
    assert.match(accept, /(?:^|,)\.gif(?:,|$)/);
    assert.match(accept, /(?:^|,)\.avif(?:,|$)/);
  });
});

test("public: long lists are assembled off-DOM and committed once", () => {
  for (const name of ["renderProjects", "renderHistory", "renderMessages"]) {
    const render = functionSource(publicSource, name);
    assert.match(render, /document\.createDocumentFragment\(\)/, name);
    assert.match(render, /fragment\.append\(/, name);
    assert.match(render, /replaceChildren\(fragment\)/, name);
    assert.ok(
      render.indexOf("fragment.append(") < render.indexOf("replaceChildren(fragment)"),
      `${name} should commit after building`,
    );
  }
});

test("public: routine message renders avoid forced composer layout reads", () => {
  const render = functionSource(publicSource, "renderMessages");
  assert.match(render, /const composerWrap = animateComposer \?/);
  assert.match(render, /const previousComposerTop = composerWrap\?\.getBoundingClientRect\(\)\.top \|\| 0/);
  assert.doesNotMatch(render, /const composerWrap = elements\.composer\.closest/);
});

test("public: cloud sync reuses its serialized snapshot", () => {
  const sync = functionSource(publicSource, "syncCloudConversations");
  assert.match(sync, /body: outgoingSignature/);
  assert.match(sync, /lastCloudSignature = changed \? cloudSignature\(\) : outgoingSignature/);
  assert.doesNotMatch(sync, /body: JSON\.stringify\(outgoing\)/);
});

test("public: model refresh and theme changes avoid redundant full work", () => {
  assert.match(publicSource, /updateEfforts\(\{ persist: false \}\)/);
  const efforts = functionSource(publicSource, "updateEfforts");
  assert.match(efforts, /if \(persist\) saveState\(\)/);
  const themeStart = publicSource.indexOf('elements.themeButton.addEventListener("click"');
  const themeEnd = publicSource.indexOf('elements.settingsButton.addEventListener("click"', themeStart);
  assert.ok(themeStart >= 0 && themeEnd > themeStart);
  const themeHandler = publicSource.slice(themeStart, themeEnd);
  assert.match(themeHandler, /document\.documentElement\.dataset\.theme = state\.theme/);
  assert.doesNotMatch(themeHandler, /renderAll\(\)/);
});

const runtimeSource = fs.readFileSync(sources[1][1], "utf8");

function loadRuntimeRecoveryHelpers({ storageSetFailures = 0 } = {}) {
  const updates = [];
  const storage = new Map();
  let remainingStorageFailures = storageSetFailures;
  const localStorage = {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) {
      if (remainingStorageFailures > 0) {
        remainingStorageFailures -= 1;
        throw new Error("quota exceeded");
      }
      storage.set(key, String(value));
    },
    removeItem(key) { storage.delete(key); },
  };
  const context = vm.createContext({
    AbortController,
    DOMException,
    TextDecoder,
    TextEncoder,
    clearTimeout,
    encodeURIComponent,
    setTimeout,
    localStorage,
    storage,
    updates,
  });
  vm.runInContext(`
    const TURN_RESUME_MAX_ATTEMPTS = 8;
    const TURN_RESUME_BASE_DELAY_MS = 750;
    const TURN_RESUME_MAX_DELAY_MS = 15000;
    const TURN_RESUME_RECORD_TTL_MS = 6 * 60 * 60 * 1000;
    const TURN_REPLAY_SAFETY_MARGIN_MS = 15 * 60 * 1000;
    const TURN_REPLAY_SAFE_WINDOW_MS = TURN_RESUME_RECORD_TTL_MS - TURN_REPLAY_SAFETY_MARGIN_MS;
    const pendingTurnStorageKey = "test:pending-turn";
    let activeTurn = null;
    let pendingTurnSaveTimer = null;
    let latestPendingTurnSnapshot = null;
    function saveState() {}
    function normalizeTokenUsage(value) { return value || null; }
    function normalizeCost(value) { return value || null; }
    function stripInternalAnnotations(value) { return String(value || ""); }
    function updateStreamingMessage(message) {
      updates.push({ content: message.content, recoveryStatus: message.recoveryStatus || "" });
    }
    ${functionSource(runtimeSource, "userFacingError")}
    ${functionSource(runtimeSource, "isTurnConnectionError")}
    ${functionSource(runtimeSource, "turnResumeDelay")}
    ${functionSource(runtimeSource, "turnResumeUrl")}
    ${functionSource(runtimeSource, "initialPostReplayBlockReason")}
    ${functionSource(runtimeSource, "missingTurnRecoveryError")}
    ${functionSource(runtimeSource, "acceptTurnEvent")}
    ${functionSource(runtimeSource, "buildPendingTurnSnapshot")}
    ${functionSource(runtimeSource, "flushPendingTurnSnapshot")}
    ${functionSource(runtimeSource, "persistPendingTurn")}
    ${functionSource(runtimeSource, "loadPendingTurn")}
    ${functionSource(runtimeSource, "abortError")}
    ${functionSource(runtimeSource, "consumeStream")}
    ${functionSource(runtimeSource, "consumeStreamResponse")}
    ${functionSource(runtimeSource, "applyTurnEvent")}
  `, context);
  return { context, storage, updates };
}

function ndjsonResponse(events) {
  const payload = new TextEncoder().encode(`${events.map((event) => JSON.stringify(event)).join("\n")}\n`);
  let reads = 0;
  return {
    body: {
      getReader() {
        return {
          async read() {
            reads += 1;
            if (reads === 1) return { value: payload, done: false };
            return { value: undefined, done: true };
          },
        };
      },
    },
  };
}

test("runtime: exact network error text is localized", () => {
  const { context } = loadRuntimeRecoveryHelpers();
  assert.equal(context.userFacingError("network error"), "网络连接中断，请检查网络后重试");
});

test("runtime: one stable client turn id is sent and reused for recovery", () => {
  const send = functionSource(runtimeSource, "sendMessage");
  assert.match(send, /const clientTurnId = randomId\(\)/);
  assert.match(send, /client_turn_id:\s*clientTurnId/);
  assert.match(send, /const requestKind = backendKey\.startsWith\("external:"\)[\s\S]*?isDirectImageRequest/);
  assert.match(send, /resumeEnabled:\s*\["external", "direct_image"\]\.includes\(requestKind\)/);
  assert.match(send, /consumeStream\([\s\S]*?clientTurnId,/);
});

test("runtime: ordinary Codex turns never enter the resumable transport", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  let resumeCalls = 0;
  await assert.rejects(
    context.consumeStream(null, { codexThreadIds: [] }, { content: "", files: [], streaming: true }, {
      clientTurnId: "c".repeat(32),
      initialError: new TypeError("network error"),
      resumeEnabled: false,
      waitBeforeResume: async () => {},
      requestResume: async () => {
        resumeCalls += 1;
        return ndjsonResponse([]);
      },
    }),
    /network error/,
  );
  assert.equal(resumeCalls, 0);
});

test("runtime: interrupted streams resume from their cursor without duplicate deltas or files", async () => {
  const { context, updates } = loadRuntimeRecoveryHelpers();
  const conversation = { codexThreadIds: [] };
  const assistant = { content: "", files: [], streaming: true, clientTurnId: "turn-1" };
  const initial = ndjsonResponse([
    { type: "started", threadId: "external", turnId: "turn", mode: "external", cursor: 1, event_id: "start" },
    { type: "delta", text: "第一段", cursor: 2, event_id: "delta-1" },
  ]);
  let resumeCalls = 0;
  await context.consumeStream(initial, conversation, assistant, {
    clientTurnId: "turn-1",
    maxAttempts: 2,
    waitBeforeResume: async () => {},
    requestResume: async (url) => {
      resumeCalls += 1;
      assert.match(url, /\/api\/turn\/resume\/turn-1\?cursor=2$/);
      return ndjsonResponse([
        { type: "delta", text: "第一段", cursor: 2, event_id: "delta-1" },
        { type: "delta", text: "第二段", cursor: 3, event_id: "delta-2" },
        { type: "done", files: [{ id: "file-1" }], cursor: 4, event_id: "done" },
      ]);
    },
  });
  assert.equal(resumeCalls, 1);
  assert.equal(assistant.content, "第一段第二段");
  assert.equal(assistant.files.length, 1);
  assert.equal(assistant.files[0].id, "file-1");
  assert.ok(updates.some((item) => item.recoveryStatus.includes("正在恢复回答（1/2）")));
});

test("runtime: a missing initial POST is resent once with the same client turn id", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const clientTurnId = "a".repeat(32);
  const requestPayload = {
    client_turn_id: clientTurnId,
    session_id: "b".repeat(32),
    client_conversation_id: "c".repeat(32),
    message: "只发送一次",
    history: [],
    model: "external:test:model",
  };
  const initialError = new TypeError("network error");
  let resumeCalls = 0;
  let postCalls = 0;
  const assistant = { content: "", files: [], streaming: true, clientTurnId };

  await context.consumeStream(null, { codexThreadIds: [] }, assistant, {
    clientTurnId,
    initialError,
    maxAttempts: 2,
    waitBeforeResume: async () => {},
    streamState: {
      cursor: 0,
      hasCursor: false,
      eventIds: new Set(),
      finished: false,
      initialPostUncertain: true,
      initialPostResponseState: "no_http_response",
      initialPostAttemptedAt: Date.now(),
      initialPostReplayAttempted: false,
      requestKind: "external",
      requestPayload,
    },
    requestResume: async () => {
      resumeCalls += 1;
      const error = new Error("not found");
      error.status = 404;
      throw error;
    },
    requestInitial: async (received) => {
      postCalls += 1;
      assert.equal(JSON.stringify(received), JSON.stringify(requestPayload));
      assert.equal(received.client_turn_id, clientTurnId);
      return ndjsonResponse([{ type: "done", files: [], cursor: 1, event_id: "done" }]);
    },
  });

  assert.equal(resumeCalls, 1);
  assert.equal(postCalls, 1);
});

test("runtime: refresh keeps the original POST data for the same 404 recovery path", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const clientTurnId = "d".repeat(32);
  const conversation = { id: "e".repeat(32), backendKey: "external:test", codexThreadIds: [] };
  const assistant = {
    id: "f".repeat(32), content: "", files: [], streaming: true, clientTurnId, createdAt: 1,
  };
  const requestPayload = {
    client_turn_id: clientTurnId,
    session_id: "1".repeat(32),
    client_conversation_id: conversation.id,
    message: "刷新后补发",
    history: [{ id: "2".repeat(32), role: "user", content: "上下文" }],
    model: "external:test:model",
  };
  context.persistPendingTurn(conversation, assistant, {
    cursor: 0,
    hasCursor: false,
    eventIds: new Set(),
    initialPostUncertain: true,
    initialPostResponseState: "no_http_response",
    initialPostAttemptedAt: Date.now(),
    initialPostReplayAttempted: false,
    requestKind: "external",
    requestPayload,
  }, { immediate: true });
  const pending = context.loadPendingTurn();
  assert.equal(pending.requestPayloadStored, true);
  assert.equal(JSON.stringify(pending.requestPayload), JSON.stringify(requestPayload));

  let resent = 0;
  await context.consumeStream(null, conversation, assistant, {
    clientTurnId,
    initialError: new TypeError("network error"),
    maxAttempts: 1,
    waitBeforeResume: async () => {},
    streamState: {
      cursor: pending.cursor,
      hasCursor: pending.hasCursor,
      eventIds: new Set(pending.eventIds),
      finished: false,
      initialPostUncertain: pending.initialPostUncertain,
      initialPostResponseState: pending.initialPostResponseState,
      initialPostAttemptedAt: pending.initialPostAttemptedAt,
      initialPostReplayAttempted: pending.initialPostReplayAttempted,
      requestKind: pending.requestKind,
      requestPayload: pending.requestPayload,
    },
    requestResume: async () => {
      const error = new Error("not found");
      error.status = 404;
      throw error;
    },
    requestInitial: async (received) => {
      resent += 1;
      assert.equal(JSON.stringify(received), JSON.stringify(requestPayload));
      return ndjsonResponse([{ type: "done", files: [], cursor: 1, event_id: "done" }]);
    },
  });
  assert.equal(resent, 1);
});

test("runtime: a missing direct-image POST is resent once with the same client turn id", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const clientTurnId = "6".repeat(32);
  const requestPayload = {
    client_turn_id: clientTurnId,
    session_id: "7".repeat(32),
    client_conversation_id: "8".repeat(32),
    message: "请生成一张小猫图片",
    history: [],
    model: "gpt-5.6",
    attachments: [],
    project_attachments: [],
  };
  const streamState = {
    cursor: 0,
    hasCursor: false,
    eventIds: new Set(),
    finished: false,
    initialPostUncertain: true,
    initialPostResponseState: "no_http_response",
    initialPostAttemptedAt: Date.now(),
    initialPostReplayAttempted: false,
    requestKind: "direct_image",
    requestPayload,
  };
  let resent = 0;
  await context.consumeStream(null, { codexThreadIds: [] }, { content: "", files: [], streaming: true }, {
    clientTurnId,
    initialError: new TypeError("network error"),
    maxAttempts: 1,
    waitBeforeResume: async () => {},
    streamState,
    resumeEnabled: true,
    requestResume: async () => {
      const error = new Error("not found");
      error.status = 404;
      throw error;
    },
    requestInitial: async (received) => {
      resent += 1;
      assert.equal(received.client_turn_id, clientTurnId);
      return ndjsonResponse([{ type: "done", files: [], cursor: 1, event_id: "done" }]);
    },
  });
  assert.equal(resent, 1);
  assert.equal(streamState.initialPostReplayAttempted, true);
  assert.equal(streamState.initialPostResponseState, "received");
});

test("runtime: refresh preserves direct-image replay eligibility", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const clientTurnId = "9".repeat(32);
  const conversation = { id: "a".repeat(32), backendKey: "codex", codexThreadIds: [] };
  const assistant = { id: "b".repeat(32), content: "", files: [], streaming: true, clientTurnId, createdAt: 1 };
  const requestPayload = {
    client_turn_id: clientTurnId,
    session_id: "c".repeat(32),
    client_conversation_id: conversation.id,
    message: "请生成一张海报",
    history: [],
    model: "gpt-5.6",
    attachments: [],
    project_attachments: [],
  };
  context.persistPendingTurn(conversation, assistant, {
    cursor: 0,
    hasCursor: false,
    eventIds: new Set(),
    initialPostUncertain: true,
    initialPostResponseState: "no_http_response",
    initialPostAttemptedAt: Date.now(),
    initialPostReplayAttempted: false,
    requestKind: "direct_image",
    requestPayload,
  }, { immediate: true });
  const pending = context.loadPendingTurn();
  assert.equal(pending.requestKind, "direct_image");
  assert.equal(pending.requestPayloadStored, true);
  let resent = 0;
  await context.consumeStream(null, conversation, assistant, {
    clientTurnId,
    initialError: new TypeError("network error"),
    maxAttempts: 1,
    waitBeforeResume: async () => {},
    streamState: {
      cursor: pending.cursor,
      hasCursor: pending.hasCursor,
      eventIds: new Set(pending.eventIds),
      finished: false,
      initialPostUncertain: pending.initialPostUncertain,
      initialPostResponseState: pending.initialPostResponseState,
      initialPostAttemptedAt: pending.initialPostAttemptedAt,
      initialPostReplayAttempted: pending.initialPostReplayAttempted,
      requestKind: pending.requestKind,
      requestPayload: pending.requestPayload,
    },
    resumeEnabled: true,
    requestResume: async () => {
      const error = new Error("not found");
      error.status = 404;
      throw error;
    },
    requestInitial: async () => {
      resent += 1;
      return ndjsonResponse([{ type: "done", files: [], cursor: 1, event_id: "done" }]);
    },
  });
  assert.equal(resent, 1);
});

test("runtime: storage pressure preserves the resume cursor before dropping replay data", () => {
  const { context } = loadRuntimeRecoveryHelpers({ storageSetFailures: 2 });
  const clientTurnId = "3".repeat(32);
  context.persistPendingTurn(
    { id: "4".repeat(32), backendKey: "external:test" },
    { id: "5".repeat(32), clientTurnId, content: "部分回答", files: [], createdAt: 1 },
    {
      cursor: 7,
      hasCursor: true,
      eventIds: new Set(["event-7"]),
      initialPostUncertain: true,
      initialPostResponseState: "no_http_response",
      initialPostAttemptedAt: Date.now(),
      initialPostReplayAttempted: false,
      requestKind: "external",
      requestPayload: { client_turn_id: clientTurnId, message: "x".repeat(1000) },
    },
    { immediate: true },
  );
  const pending = context.loadPendingTurn();
  assert.equal(pending.cursor, 7);
  assert.equal(pending.hasCursor, true);
  assert.equal(pending.storageDegraded, true);
  assert.equal(pending.requestPayloadStored, false);
  assert.equal(pending.requestPayload, null);
  assert.equal(pending.initialPostResponseState, "no_http_response");
  assert.ok(pending.initialPostAttemptedAt > 0);
});

test("runtime: legacy and near-expiry snapshots never auto-resend on 404", async () => {
  const { context, storage } = loadRuntimeRecoveryHelpers();
  const legacyClientTurnId = "d".repeat(32);
  storage.set("test:pending-turn", JSON.stringify({
    version: 2,
    clientTurnId: legacyClientTurnId,
    conversationId: "e".repeat(32),
    assistantMessageId: "f".repeat(32),
    backendKey: "external:test",
    initialPostUncertain: true,
    requestPayloadStored: true,
    requestPayload: { client_turn_id: legacyClientTurnId, message: "旧请求" },
  }));
  const legacy = context.loadPendingTurn();
  assert.equal(legacy.initialPostResponseState, "unknown");
  assert.equal(legacy.initialPostAttemptedAt, 0);

  const cases = [
    {
      name: "legacy",
      expected: /无法确认原请求是否已经执行.*核对官网或会话记录.*手动重试/,
      state: {
        initialPostUncertain: legacy.initialPostUncertain,
        initialPostResponseState: legacy.initialPostResponseState,
        initialPostAttemptedAt: legacy.initialPostAttemptedAt,
        initialPostReplayAttempted: legacy.initialPostReplayAttempted,
        requestPayload: legacy.requestPayload,
      },
    },
    {
      name: "near expiry",
      expected: /接近或超过 6 小时恢复期限.*核对官网或会话记录.*手动重试/,
      state: {
        initialPostUncertain: true,
        initialPostResponseState: "no_http_response",
        initialPostAttemptedAt: Date.now() - ((6 * 60 * 60 * 1000) - (15 * 60 * 1000)),
        initialPostReplayAttempted: false,
        requestPayload: { client_turn_id: "1".repeat(32), message: "临近期限" },
      },
    },
    {
      name: "confirmed response",
      expected: /已经收到过服务器响应.*核对官网或会话记录.*手动重试/,
      state: {
        initialPostUncertain: false,
        initialPostResponseState: "received",
        initialPostAttemptedAt: Date.now(),
        initialPostReplayAttempted: false,
        requestPayload: null,
      },
    },
  ];

  for (const item of cases) {
    let resent = 0;
    await assert.rejects(
      context.consumeStream(null, { codexThreadIds: [] }, { content: "", files: [], streaming: true }, {
        clientTurnId: "2".repeat(32),
        initialError: new TypeError("network error"),
        maxAttempts: 1,
        waitBeforeResume: async () => {},
        streamState: {
          cursor: 0,
          hasCursor: false,
          eventIds: new Set(),
          finished: false,
          requestKind: "external",
          ...item.state,
        },
        requestResume: async () => {
          const error = new Error("not found");
          error.status = 404;
          throw error;
        },
        requestInitial: async () => {
          resent += 1;
          return ndjsonResponse([]);
        },
      }),
      item.expected,
      item.name,
    );
    assert.equal(resent, 0, item.name);
  }
});

test("runtime: a failed safe replay is never submitted a second time", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  let resumeCalls = 0;
  let resendCalls = 0;
  await assert.rejects(
    context.consumeStream(null, { codexThreadIds: [] }, { content: "", files: [], streaming: true }, {
      clientTurnId: "3".repeat(32),
      initialError: new TypeError("network error"),
      maxAttempts: 2,
      waitBeforeResume: async () => {},
      streamState: {
        cursor: 0,
        hasCursor: false,
        eventIds: new Set(),
        finished: false,
        initialPostUncertain: true,
        initialPostResponseState: "no_http_response",
        initialPostAttemptedAt: Date.now(),
        initialPostReplayAttempted: false,
        requestKind: "external",
        requestPayload: { client_turn_id: "3".repeat(32), message: "只补发一次" },
      },
      requestResume: async () => {
        resumeCalls += 1;
        const error = new Error("not found");
        error.status = 404;
        throw error;
      },
      requestInitial: async () => {
        resendCalls += 1;
        throw new TypeError("network error");
      },
    }),
    /自动补发已经尝试过.*不会再次补发/,
  );
  assert.equal(resumeCalls, 2);
  assert.equal(resendCalls, 1);
});

test("runtime: exhausted recovery attempts produce an actionable Chinese error", async () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const assistant = { content: "已收到的部分", files: [], streaming: true, clientTurnId: "turn-2" };
  let resumeCalls = 0;
  await assert.rejects(
    context.consumeStream(ndjsonResponse([
      { type: "delta", text: "内容", cursor: 1, event_id: "delta" },
    ]), { codexThreadIds: [] }, assistant, {
      clientTurnId: "turn-2",
      maxAttempts: 2,
      waitBeforeResume: async () => {},
      requestResume: async () => {
        resumeCalls += 1;
        throw new TypeError("network error");
      },
    }),
    (error) => error?.recoveryFailed === true && /暂时无法连接.*刷新页面会继续取回.*请勿重复发送/.test(error.message),
  );
  assert.equal(resumeCalls, 2);
  assert.equal(assistant.content, "已收到的部分内容");
  assert.equal(assistant.recoveryStatus, "");
});

test("runtime: mobile recovery uses a bounded roughly one-minute retry window", () => {
  const { context } = loadRuntimeRecoveryHelpers();
  const delays = Array.from({ length: 8 }, (_, index) => context.turnResumeDelay(index));
  assert.deepEqual([...delays], [750, 1500, 3000, 6000, 12000, 15000, 15000, 15000]);
  assert.equal(delays.reduce((total, value) => total + value, 0), 68_250);
});

test("runtime: foreground and online recovery share one in-flight attempt", async () => {
  const stats = { calls: 0 };
  const context = vm.createContext({ stats });
  vm.runInContext(`
    let pendingRecoveryPromise = null;
    let finishRecovery = null;
    function resumePendingTurn() {
      stats.calls += 1;
      return new Promise((resolve) => { finishRecovery = resolve; });
    }
    function releaseRecovery() { finishRecovery?.(); }
    ${functionSource(runtimeSource, "resumePendingTurnOnce")}
  `, context);

  const first = context.resumePendingTurnOnce();
  const second = context.resumePendingTurnOnce();
  assert.equal(first, second);
  assert.equal(stats.calls, 1);
  context.releaseRecovery();
  await first;

  const third = context.resumePendingTurnOnce();
  assert.equal(stats.calls, 2);
  context.releaseRecovery();
  await third;
});

test("runtime: pending turns survive refresh and reconnect through the resume endpoint", () => {
  const restore = functionSource(runtimeSource, "resumePendingTurn");
  assert.match(restore, /const pending = loadPendingTurn\(\)/);
  assert.match(restore, /cursor:\s*Math\.max\(0, Number\(pending\.cursor\) \|\| 0\)/);
  assert.match(restore, /eventIds:\s*new Set\(Array\.isArray\(pending\.eventIds\)/);
  assert.match(restore, /consumeStream\(null,[\s\S]*?clientTurnId:\s*pending\.clientTurnId/);
  assert.match(runtimeSource, /window\.addEventListener\("pagehide", flushPendingTurnSnapshot\)/);
  assert.match(runtimeSource, /window\.addEventListener\("online", \(\) => \{[\s\S]*?if \(pendingRecoveryWaiting\) \{[\s\S]*?resumePendingTurnOnce\(\)/);
  assert.match(runtimeSource, /document\.addEventListener\("visibilitychange", \(\) => \{[\s\S]*?!document\.hidden && pendingRecoveryWaiting[\s\S]*?resumePendingTurnOnce\(\)/);
  assert.match(runtimeSource, /void resumePendingTurnOnce\(\);\s*syncCloudConversations/);
  assert.match(runtimeSource, /if \(!keepPendingRecovery\) clearPendingTurn\(clientTurnId\)/);
  assert.match(runtimeSource, /暂时无法连接；联网后刷新页面会继续取回，请勿重复发送/);
});

test("runtime: an unconfirmed stop keeps the pending turn recoverable", async () => {
  const stats = { aborts: 0, clears: 0, messages: [] };
  const context = vm.createContext({ stats });
  vm.runInContext(`
    let isSending = false;
    let pendingRecoveryWaiting = true;
    let activeTurn = { threadId: "external", turnId: "${"a".repeat(32)}" };
    let activeTurnRequest = { abort() { stats.aborts += 1; } };
    const elements = { stopButton: { disabled: false } };
    async function api() { throw new TypeError("network error"); }
    function userFacingError() { return "网络连接中断，请检查网络后重试"; }
    function showComposerError(message) { stats.messages.push(message); }
    function clearPendingTurn() { stats.clears += 1; }
    ${functionSource(runtimeSource, "stopCurrentTurn").split('\nelements.menuButton')[0]}
    function recoveryState() { return { isSending, pendingRecoveryWaiting, activeTurn }; }
  `, context);

  await context.stopCurrentTurn();
  assert.equal(stats.aborts, 0);
  assert.equal(stats.clears, 0);
  assert.equal(context.recoveryState().pendingRecoveryWaiting, true);
  assert.match(stats.messages[0], /未能确认停止.*仍会继续恢复/);
});

test("runtime: recovery waiting protects the conversation from deletion and other key actions", async () => {
  const conversationId = "b".repeat(32);
  const appState = { conversations: [{ id: conversationId }] };
  const notices = [];
  const context = vm.createContext({ appState, notices });
  vm.runInContext(`
    let pendingRecoveryWaiting = true;
    let isSending = false;
    const state = appState;
    const attachments = [];
    function showComposerError(message) { notices.push(message); }
    ${functionSource(runtimeSource, "blockPendingRecoveryAction")}
    ${functionSource(runtimeSource, "deleteConversation")}
  `, context);

  await context.deleteConversation(conversationId);
  assert.equal(appState.conversations.length, 1);
  assert.match(notices[0], /正在恢复未完成的回答.*不能删除聊天/);
  assert.match(functionSource(runtimeSource, "selectProject"), /blockPendingRecoveryAction\("切换项目"\)/);
  assert.match(functionSource(runtimeSource, "createConversation"), /blockPendingRecoveryAction\("新建聊天"\)/);
  assert.match(functionSource(runtimeSource, "uploadSelectedFiles"), /blockPendingRecoveryAction\("上传文件"\)/);
  assert.match(functionSource(runtimeSource, "uploadProjectFiles"), /blockPendingRecoveryAction\("上传项目文件"\)/);
});
