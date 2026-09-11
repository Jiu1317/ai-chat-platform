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
    classList: { remove() {} },
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
    function renderMarkdown(value) {
      stats.renders += 1;
      return String(value);
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
  const consume = functionSource(publicSource, "consumeStream");
  assert.match(consume, /assistantMessage\.content \+= String\(event\.text \|\| ""\)/);
  assert.doesNotMatch(
    consume,
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
