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
