import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const baseURL = process.env.VIV_BASE_URL || "http://127.0.0.1:18041";
const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "chrome" });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
await page.addInitScript(() => {
  const originalFetch = window.fetch.bind(window);
  window.__workflowRequests = [];
  window.fetch = async (path, options) => {
    if (options?.body instanceof FormData) {
      const fields = {};
      for (const [key, value] of options.body) if (typeof value === "string") fields[key] = value;
      const file = options.body.get("file");
      const bytes = file ? [...new Uint8Array(await file.arrayBuffer())] : [];
      window.__workflowRequests.push({ path, fields, bytes });
    }
    return originalFetch(path, options);
  };
});
const errors = [];
const checks = [];
page.on("pageerror", (error) => errors.push(error.message));
page.setDefaultTimeout(15000);

async function idle() {
  await page.waitForFunction(() => document.querySelector("main").getAttribute("aria-busy") === "false");
  assert.equal(await page.locator("#error").isVisible(), false, await page.locator("#error").textContent());
}
async function previewAfter(action) {
  const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/workflow/preview"));
  await action();
  const response = await responsePromise;
  assert.equal(response.status(), 200, await response.text());
  const data = await response.json();
  await idle();
  const posted = await page.evaluate(() => window.__workflowRequests.filter((item) => item.path.endsWith("/preview")).at(-1));
  return { data, posted, request: response.request() };
}
async function readDownload(download) {
  assert.equal(await download.failure(), null);
  const chunks = [];
  for await (const chunk of await download.createReadStream()) chunks.push(chunk);
  return Buffer.concat(chunks);
}
function rateRows(rates) {
  return Array.isArray(rates) ? rates : Object.entries(rates).map(([key, row]) => {
    const [mcc, mnc] = key.split(":");
    return { mcc, mnc, ...row };
  });
}
async function confirmPreview() {
  await page.locator("#confirmed").check();
  if (await page.locator("#partial-confirmation").isVisible()) await page.locator("#allow-partial").check();
  if (await page.locator("#increase-confirmation").isVisible()) await page.locator("#confirm-increases").check();
}

try {
  await page.goto(baseURL);
  await idle();
  assert.match(await page.title(), /影核.*智能核价/);
  assert.equal(await page.locator("#supplier").inputValue(), "standard-sms");
  assert.match(await page.locator('#baseline-form label').filter({ has: page.locator('[name="effective_time"]') }).textContent(), /GMT\+8/);
  assert.match(await page.locator('#baseline-supplier').textContent(), /直接按 GMT\+8/);
  const profiles = await (await context.request.get(baseURL + "/api/suppliers")).json();
  for (const id of ["tencent-sms", "volcano-sms"]) {
    const profile = profiles.suppliers.find((item) => item.id === id);
    assert.ok(profile, "应动态加载腾讯和火山供应商种子配置");
    await page.locator("#supplier").selectOption(id);
    await idle();
    assert.equal(await page.locator("#sheet-name").inputValue(), profile.sheet_name || "");
    assert.deepEqual(JSON.parse(await page.locator('[name="country_map"]').inputValue()), profile.country_map || {});
    if (!profile.headers.mnc) assert.equal(await page.locator('[name="header_mnc"]').getAttribute("required"), null);
    assert.match(await page.locator("#rule-summary").textContent(), new RegExp(profile.currency));
  }
  await page.locator("#supplier").selectOption("standard-sms");
  await idle();
  checks.push("腾讯国家映射与火山 MCC-only 配置由接口动态加载，币种及默认工作表正确显示");
  const initial = await previewAfter(() => page.locator('[data-example="sms_both"]').click());
  assert.equal(await page.locator("#email-date").inputValue(), "");
  assert.ok(initial.data.email.date_candidates.length > 0);
  assert.equal(initial.posted.fields.email_date, "");
  assert.equal(initial.posted.fields.sheet_name, "Pricing_");
  const chosen = initial.data.email.date_candidates[0].value;
  const selected = await previewAfter(() => page.locator("#email-date").selectOption(chosen));
  assert.equal(selected.posted.fields.email_date, chosen);
  assert.ok(selected.data.stats.price_increases > 0, "短信样例应展示涨价");
  await page.locator("#confirmed").check();
  if (await page.locator("#partial-confirmation").isVisible()) await page.locator("#allow-partial").check();
  assert.equal(await page.locator("#export").isDisabled(), true, "未确认涨价不得导出");
  await page.locator("#confirm-increases").check();
  assert.equal(await page.locator("#export").isEnabled(), true);
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloadPromise;
  const workbook = await readDownload(download);
  await idle();
  assert.match(download.suggestedFilename(), /\.xls$/i);
  assert.equal(workbook.subarray(0, 8).toString("hex"), "d0cf11e0a1b11ae1", "应为真实 BIFF8 xls");
  const importPromise = page.waitForResponse((response) => response.url().endsWith("/api/workflow/import"));
  await page.locator("#import").click();
  const imported = await importPromise;
  assert.equal(imported.status(), 200, await imported.text());
  const importBody = await page.evaluate(() => window.__workflowRequests.filter((item) => item.path.endsWith("/import")).at(-1));
  assert.deepEqual(Buffer.from(importBody.bytes), workbook, "必须提交刚下载的完整文件字节");
  assert.ok(importBody.fields.export_proof, "必须提交导出绑定凭据");
  const receipt = await imported.json();
  assert.equal(receipt.mode, "mock");
  assert.ok(receipt.imported > 0);
  await idle();
  assert.match(await page.locator("#receipt-summary").textContent(), new RegExp(receipt.receipt_id));
  assert.equal(await page.locator("#import").isDisabled(), true);
  const rates = await (await context.request.get(baseURL + "/api/mock/rates?supplier_id=standard-sms")).json();
  const expectedRow = selected.data.rows.find((row) => row.eligible && row.price_increased);
  const updated = rateRows(rates.rates).find((row) => row.mcc === expectedRow.mcc && row.mnc === expectedRow.mnc);
  assert.equal(Number(updated.downstream), Number(expectedRow.downstream));
  assert.equal(Number(updated.upstream), Number(expectedRow.upstream));
  checks.push("短信双向 → 邮件日期显式选择 → 涨价确认 → 真实 xls 下载 → 原文件导入 → 回执及价格更新");

  await page.locator("#supplier").selectOption("skyline-sms");
  await idle();
  assert.equal(await page.locator("#preview-section").isVisible(), false);
  assert.equal(await page.locator("#result").isVisible(), false);
  assert.equal(await page.locator("#import").isDisabled(), true);
  assert.equal(await page.locator("#download-result").getAttribute("href"), null);
  checks.push("切换供应商立即清空旧预览、导出及导入权限");

  await previewAfter(() => page.locator('[data-example="sms_both"]').click());
  await page.locator("#email-file").setInputFiles([]);
  await page.locator("#email-text").fill("此报价于 2026-11-20 09:00:00 UTC 生效。");
  assert.equal(await page.locator("#result").isVisible(), false);
  const single = await previewAfter(() => page.locator("#preview").click());
  assert.equal(single.data.email.date_candidates.length, 1);
  assert.equal(await page.locator("#email-date").inputValue(), "", "单候选不得自动选择");
  const selectedSingle = await previewAfter(() => page.locator("#email-date").selectOption(single.data.email.date_candidates[0].value));
  assert.equal(selectedSingle.posted.fields.email_date, single.data.email.date_candidates[0].value);
  await page.locator("#manual-date").fill("2026-11-22T10:30");
  assert.equal(await page.locator("#preview-section").isVisible(), false);
  assert.equal(await page.locator("#email-date").inputValue(), "");
  checks.push("邮件仅单候选也需显式选择；手填日期清空旧结果");

  await page.locator("#config-details > summary").click();
  await page.locator('#supplier-form [name="id"]').fill("browser-test-country");
  await page.locator('#supplier-form [name="name"]').fill("界面检查国家映射");
  await page.locator("#special-mapping > summary").click();
  await page.locator("#mapping-mode").selectOption("country");
  await page.locator('[name="header_country"]').fill("Code");
  await page.locator('[name="country_map"]').fill('{"US":{"mcc":"310","mnc":"999"}}');
  assert.equal(await page.locator("#preview").isDisabled(), true);
  const savePromise = page.waitForResponse((response) => response.url().endsWith("/api/suppliers") && response.request().method() === "POST");
  await page.locator("#save-supplier").click();
  const saved = await savePromise;
  assert.equal(saved.status(), 200, await saved.text());
  const profile = saved.request().postDataJSON();
  assert.equal(profile.headers.country, "Code");
  assert.equal(profile.headers.mcc, "");
  assert.equal(profile.country_map.US.mcc, "310");
  await idle();
  assert.equal(await page.locator("#supplier").inputValue(), "browser-test-country");
  checks.push("新建供应商、五列表头及特殊国家映射保存");

  await page.locator("#supplier").selectOption("standard-sms");
  await idle();
  await page.locator("#baseline-details > summary").click();
  const operator = page.locator("#operator-form");
  await operator.locator('[name="mcc"]').fill("311");
  await operator.locator('[name="mnc"]').fill("001");
  await operator.locator('[name="name"]').fill("界面检查模拟运营商");
  await page.locator("#save-operator").click();
  await idle();
  const baseline = page.locator("#baseline-form");
  assert.equal(await baseline.locator('[name="mnc"]').inputValue(), "001");
  await baseline.locator('[name="downstream"]').fill("0.015");
  await baseline.locator('[name="upstream"]').fill("0.002");
  await baseline.locator('[name="effective_time"]').fill("2026-11-20T09:00");
  await baseline.locator('[name="currency"]').fill("EUR");
  assert.equal(await page.locator("#save-baseline").isDisabled(), true);
  await page.locator("#baseline-confirmed").check();
  await page.locator("#save-baseline").click();
  await idle();
  const maintained = await (await context.request.get(baseURL + "/api/mock/rates?supplier_id=standard-sms")).json();
  assert.ok(rateRows(maintained.rates).some((row) => row.mcc === "311" && row.mnc === "001" && Number(row.downstream) === 0.015));
  const isolated = await browser.newContext();
  const otherRates = await (await isolated.request.get(baseURL + "/api/mock/rates?supplier_id=standard-sms")).json();
  assert.ok(!rateRows(otherRates.rates).some((row) => row.mcc === "311" && row.mnc === "001"));
  await isolated.close();
  checks.push("显式维护合法编码与基准价格；不同浏览器工作区隔离");

  await previewAfter(() => page.locator('[data-example="sms_mt"]').click());
  assert.equal(await page.locator("#direction").inputValue(), "mt");
  await previewAfter(() => page.locator('[data-example="sms_mo"]').click());
  assert.equal(await page.locator("#direction").inputValue(), "mo");
  checks.push("短信下行与上行示例可独立预览");

  await page.locator("#config-details > summary").click();
  const voice = await previewAfter(() => page.locator('[data-example="voice"]').click());
  assert.match(await page.locator("#direction").textContent(), /呼出/);
  assert.equal(await page.locator("#voice-note").isVisible(), true);
  assert.match(await page.locator("#code-heading").textContent(), /CC.*演示/);
  if (voice.data.email.date_candidates.length) await previewAfter(() => page.locator("#email-date").selectOption(voice.data.email.date_candidates[0].value));
  await confirmPreview();
  assert.equal(await page.locator("#export").isEnabled(), true, "语音示例应可导出");
  const voiceDownloadPromise = page.waitForEvent("download");
  await page.locator("#export").click();
  const voiceWorkbook = await readDownload(await voiceDownloadPromise);
  assert.equal(voiceWorkbook.subarray(0, 8).toString("hex"), "d0cf11e0a1b11ae1");
  await idle();
  await page.locator("#import").click();
  await idle();
  assert.equal(await page.locator("#receipt").isVisible(), true);
  checks.push("语音演示预览、真实 xls 导出及模拟导入回执，明确待真实模板复核");
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForFunction(() => document.documentElement.scrollWidth <= window.innerWidth);
  await page.locator("#config-details > summary").click();
  await page.waitForFunction(() => document.documentElement.scrollWidth <= window.innerWidth);
  await page.locator("#config-details > summary").click();
  assert.equal(await page.locator("#preview-body tr").count() <= 100, true);
  if (process.env.VIV_SCREENSHOT_DIR) {
    const directory = resolve(process.env.VIV_SCREENSHOT_DIR);
    await mkdir(directory, { recursive: true });
    await page.screenshot({ path: resolve(directory, "workflow-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: resolve(directory, "workflow-desktop.png"), fullPage: true });
  }
  assert.deepEqual(errors, []);
  checks.push("375px 无页面横向溢出、预览最多100行、无浏览器脚本错误");

  await page.route("**/api/workflow/preview", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.rows = Array.from({ length: 105 }, (_, index) => ({ ...data.rows[0], source_row: index + 2,
      mcc: index === 0 ? "=SUM(1,1)" : "310", mnc: "001", eligible: true,
      notes: [index === 0 ? '<img src=x onerror="alert(1)">' : "模拟检查提醒"], errors: [], price_increased: index === 0 }));
    data.issues = [];
    data.stats = { ...data.stats, total: 105, exported: 105, blocked: 0, warnings: 105, price_increases: 1 };
    await route.fulfill({ response, json: data });
  });
  await previewAfter(() => page.locator("#preview").click());
  assert.equal(await page.locator("#preview-body tr").count(), 100);
  assert.equal(await page.locator("#preview-body img").count(), 0);
  await page.locator("#row-filter").selectOption("increases");
  assert.equal(await page.locator("#preview-body tr").count(), 1);
  const csvPromise = page.waitForEvent("download");
  await page.locator("#download-issues").click();
  const csv = (await readDownload(await csvPromise)).toString("utf8");
  assert.equal(csv.split("\r\n").length, 106, "CSV 应包含完整105行，不受界面筛选影响");
  assert.ok(csv.includes("'=SUM(1,1)"), "Excel 公式注入应加单引号");
  await page.unroute("**/api/workflow/preview");
  checks.push("105行界面限100行、涨价筛选、完整CSV及公式注入防护，外部HTML仅作为文本显示");

  await previewAfter(() => page.locator('[data-example="sms_both"]').click());
  await page.locator("#manual-date").fill("2026-11-20T09:00");
  await previewAfter(() => page.locator("#preview").click());
  await confirmPreview();
  await page.route("**/api/workflow/export", async (route) => {
    const response = await route.fetch();
    const headers = response.headers();
    delete headers["x-export-proof"];
    await route.fulfill({ response, headers });
  });
  const missingProofDownload = page.waitForEvent("download");
  await page.locator("#export").click();
  await readDownload(await missingProofDownload);
  await page.waitForFunction(() => document.querySelector("main").getAttribute("aria-busy") === "false");
  assert.equal(await page.locator("#import").isDisabled(), true);
  assert.match(await page.locator("#error").textContent(), /缺少绑定凭据/);
  await page.unroute("**/api/workflow/export");
  checks.push("缺少 X-Export-Proof 时保留下载但禁止导入并明确提示");
  console.log(JSON.stringify({ ok: true, baseURL, checks }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ ok: false, checks, error: error.message, pageErrors: errors, status: await page.locator("#status").textContent(), detail: await page.locator("#error").textContent() }, null, 2));
  process.exitCode = 1;
} finally {
  await context.close();
  await browser.close();
}
