"use strict";

const byId = (id) => document.getElementById(id);
const configForm = byId("supplier-form");
const headerKeys = ["mcc", "mnc", "mt", "mo", "date", "mccmnc", "country"];
let suppliers = [];
let selectedFile = null;
let emailFile = null;
let preview = null;
let exported = null;
let busy = false;
let configDirty = false;
let revision = 0;

function textElement(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text ?? "—";
  if (className) element.className = className;
  return element;
}

function setStatus(message) { byId("status").textContent = message; }
function showError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}
function supplier() { return suppliers.find((item) => item.id === byId("supplier").value); }
function count(name) { return Number(preview?.stats?.[name] || 0); }
function notes(row) { return [...(row.errors || []), ...(row.notes || [])]; }
function hasIssue(row) { return row.eligible === false || notes(row).length > 0; }
function setStep(current) {
  ["upload", "preview", "export"].forEach((name, index) => {
    const element = byId("step-" + name);
    element.classList.toggle("active", index === current);
    element.classList.toggle("complete", index < current);
    if (index === current) element.setAttribute("aria-current", "step");
    else element.removeAttribute("aria-current");
  });
}

function clearResult() {
  if (exported) URL.revokeObjectURL(exported.url);
  exported = null;
  byId("result").hidden = true;
  byId("receipt").hidden = true;
  byId("download-result").removeAttribute("href");
  byId("result-counts").textContent = "";
  setStep(preview ? 1 : 0);
}

function invalidate(message = "输入已变化，请重新预览。", clearEmail = false) {
  revision += 1;
  preview = null;
  clearResult();
  byId("preview-section").hidden = true;
  byId("preview-body").replaceChildren();
  for (const id of ["confirmed", "confirm-increases", "allow-partial"]) byId(id).checked = false;
  if (clearEmail) {
    byId("email-date").replaceChildren(new Option("尚未选择日期", ""));
    byId("email-review").hidden = true;
  }
  showError();
  setStatus(message);
  updateControls();
}

function canExport() {
  return preview && count("exported") > 0 && byId("confirmed").checked
    && (!count("price_increases") || byId("confirm-increases").checked)
    && (!count("blocked") || byId("allow-partial").checked);
}

function updateControls() {
  document.querySelectorAll("button, input, select, textarea").forEach((element) => { element.disabled = busy; });
  const ready = Boolean(supplier());
  byId("preview").disabled = busy || !ready || !selectedFile || configDirty;
  byId("export").disabled = busy || configDirty || !canExport();
  byId("import").disabled = busy || !exported || !exported.proof || exported.imported || exported.revision !== revision;
  byId("download-issues").disabled = busy || !preview;
  byId("save-supplier").disabled = busy || !ready;
  byId("save-baseline").disabled = busy || !ready || !byId("baseline-confirmed").checked;
  byId("reset").disabled = busy || !byId("reset-confirmed").checked;
  byId("dirty-hint").hidden = !configDirty;
  byId("download-result").setAttribute("aria-disabled", String(busy || !exported));
  byId("download-result").tabIndex = busy || !exported ? -1 : 0;
  document.querySelector("main").setAttribute("aria-busy", String(busy));
}

async function request(path, options = {}) {
  let response;
  try { response = await fetch(path, { credentials: "same-origin", ...options }); }
  catch { throw new Error("网络连接失败，请检查连接后重试。"); }
  if (!response.ok) {
    let message = "请求失败，请检查文件或稍后重试。";
    try { const data = await response.json(); if (typeof data.error === "string") message = data.error; } catch {}
    throw new Error(message);
  }
  return response;
}
async function jsonRequest(path, data) {
  return (await request(path, data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data)
  })).json();
}
async function run(message, action) {
  if (busy) return;
  busy = true;
  showError();
  setStatus(message);
  updateControls();
  try { await action(); }
  catch (error) { showError(error.message || "操作失败，请稍后重试。"); setStatus("操作未完成，请查看提示后重试。"); }
  finally { busy = false; updateControls(); }
}

function populateSupplier() {
  const current = supplier();
  if (!current) return;
  for (const element of configForm.elements) {
    if (!element.name) continue;
    element.value = element.name === "country_map" ? JSON.stringify(current.country_map || {}, null, 2) : element.name.startsWith("header_")
      ? current.headers?.[element.name.slice(7)] ?? "" : current[element.name] ?? "";
  }
  configDirty = false;
  byId("sheet-name").value = current.sheet_name || "";
  byId("mapping-mode").value = current.headers?.mccmnc ? "combined" : current.headers?.country ? "country" : "split";
  updateMapping();
  const voice = current.business === "voice";
  const directions = voice ? ["双向（呼出 + 呼入）", "呼出（MT）", "呼入（MO）"] : ["双向（下行 + 上行）", "下行（MT）", "上行（MO）"];
  [...byId("direction").options].forEach((option, index) => { option.textContent = directions[index]; });
  byId("voice-note").hidden = !voice;
  byId("code-heading").textContent = voice ? "CC（演示）" : "MCC";
  byId("system-code-heading").textContent = voice ? "CC（演示）" : "MCC";
  byId("downstream-heading").textContent = voice ? "呼出新价 / 现价" : "下行新价 / 现价";
  byId("upstream-heading").textContent = voice ? "呼入新价 / 现价" : "上行新价 / 现价";
  byId("system-downstream-heading").textContent = voice ? "呼出价" : "下行价";
  byId("system-upstream-heading").textContent = voice ? "呼入价" : "上行价";
  byId("baseline-downstream-label").firstChild.textContent = voice ? "呼出价" : "下行价";
  byId("baseline-upstream-label").firstChild.textContent = voice ? "呼入价" : "上行价";
  byId("operator-form").elements.business.value = current.business;
  byId("baseline-form").elements.currency.value = current.currency;
  byId("baseline-supplier").textContent = "当前供应商：" + current.name + "；以下基准时间直接按 GMT+8 保存，不使用报价表源时区。";
  byId("date-help").textContent = "按供应商源时区 " + current.timezone + " 填写；不按本机时区转换。手填时间优先用于补齐缺失日期。";
  const dateLabels = { email_then_system: "已选邮件日期，再用系统日期", manual: "手填日期", same_file: "同文件日期", skip: "跳过空日期" };
  const codeLabels = { max: "可用最高价", contract: "合同约定价", skip: "跳过异常组合" };
  byId("rule-summary").textContent = current.name + " · " + (voice ? "语音" : "短信") + " · " + current.currency
    + " · 源价 ÷ " + current.divisor + " · " + current.timezone + "；空日期：" + dateLabels[current.missing_date_policy]
    + "；无效编码：" + codeLabels[current.invalid_code_policy] + (current.invalid_code_policy === "contract"
      ? "（" + current.contract_rate + " " + current.contract_currency + "；换算系数 " + (current.contract_fx_rate || "未设置") + "）" : "") + "。";
}

async function loadSuppliers(preferred = byId("supplier").value) {
  const data = await jsonRequest("/api/suppliers");
  suppliers = data.suppliers || [];
  byId("supplier").replaceChildren(...suppliers.map((item) => new Option(item.name + "（" + (item.business === "voice" ? "语音" : "短信") + "）", item.id)));
  if (suppliers.some((item) => item.id === preferred)) byId("supplier").value = preferred;
  else if (suppliers.some((item) => item.id === "standard-sms")) byId("supplier").value = "standard-sms";
  if (!suppliers.length) throw new Error("未找到供应商配置，请重新加载或联系维护人员。");
  populateSupplier();
}

function updateMapping() {
  const mode = byId("mapping-mode").value;
  for (const key of ["mcc", "mnc", "mccmnc", "country"]) {
    const input = configForm.elements["header_" + key];
    const active = mode === "split" ? ["mcc", "mnc"].includes(key) : mode === "combined" ? key === "mccmnc" : key === "country";
    input.required = active && key !== "mnc";
    input.closest("label").hidden = !active;
  }
  configForm.elements.country_map.closest("label").hidden = mode !== "country";
}

function addTableRow(body, values) {
  const row = document.createElement("tr");
  values.forEach((value) => row.append(textElement("td", value)));
  body.append(row);
  return row;
}
function receiptText(item) {
  return "回执 " + (item.receipt_id || item.id || "—") + " · 已导入 " + (item.imported ?? item.stats?.imported ?? "—")
    + " 条" + (item.supplier_id ? " · " + item.supplier_id : "") + (item.created_at ? " · " + item.created_at : "");
}
function rateRows(rates) {
  return Array.isArray(rates) ? rates : Object.entries(rates || {}).map(([key, row]) => {
    const [mcc, mnc] = key.split(":");
    return { mcc, mnc, ...row };
  });
}
async function refreshSystem() {
  const id = byId("supplier").value;
  byId("mock-status").textContent = "正在读取模拟价格与回执…";
  const results = await Promise.allSettled([
    jsonRequest("/api/mock/rates?supplier_id=" + encodeURIComponent(id)),
    jsonRequest("/api/mock/operators?business=" + encodeURIComponent(supplier()?.business || "sms")), jsonRequest("/api/mock/receipts")
  ]);
  if (id !== byId("supplier").value) return;
  const [ratesResult, operatorsResult, receiptsResult] = results;
  byId("rates-body").replaceChildren();
  byId("operators-body").replaceChildren();
  byId("receipts-list").replaceChildren();
  if (ratesResult.status === "fulfilled") {
    const rates = rateRows(ratesResult.value.rates);
    rates.slice(0, 100).forEach((row) => addTableRow(byId("rates-body"), [row.mcc, row.mnc, row.downstream, row.upstream, row.currency, row.effective_time]));
    byId("rates-summary").textContent = "当前供应商共 " + rates.length + " 条模拟价格；展示前 " + Math.min(rates.length, 100) + " 条。系统时间：" + (ratesResult.value.timezone || "接口未提供时区") + "。";
  } else byId("rates-summary").textContent = "模拟价格加载失败。";
  if (operatorsResult.status === "fulfilled") {
    const operators = operatorsResult.value.operators || [];
    operators.filter((item) => !item.business || item.business === supplier()?.business).slice(0, 100)
      .forEach((row) => addTableRow(byId("operators-body"), [row.mcc, row.mnc, row.name || row.operator, row.country || row.region]));
  }
  if (receiptsResult.status === "fulfilled") {
    const receipts = receiptsResult.value.receipts || [];
    receipts.slice(0, 100).forEach((item) => byId("receipts-list").append(textElement("li", receiptText(item))));
    if (!receipts.length) byId("receipts-list").append(textElement("li", "当前浏览器尚无模拟导入回执。"));
  }
  const failures = results.filter((result) => result.status === "rejected");
  byId("mock-status").textContent = failures.length ? "部分模拟数据加载失败：" + failures.map((item) => item.reason.message).join("；") : "已读取当前浏览器的模拟工作区。";
}

function multipart() {
  const data = new FormData();
  data.append("file", selectedFile, selectedFile.name);
  data.append("supplier_id", byId("supplier").value);
  data.append("direction", byId("direction").value);
  data.append("email_text", byId("email-text").value);
  if (emailFile) data.append("email_file", emailFile, emailFile.name);
  data.append("email_date", byId("email-date").value);
  data.append("manual_date", byId("manual-date").value);
  data.append("sheet_name", byId("sheet-name").value.trim());
  return data;
}

function renderRows() {
  if (!preview) return;
  const filter = byId("row-filter").value;
  const rows = (preview.rows || []).filter((row) => filter === "increases" ? row.price_increased : filter === "issues" ? hasIssue(row) : true);
  byId("preview-body").replaceChildren();
  rows.slice(0, 100).forEach((row) => {
    const element = addTableRow(byId("preview-body"), [row.source_row, row.mcc, row.mnc]);
    element.className = row.eligible === false ? "row-blocked" : row.price_increased ? "row-increase" : "";
    for (const key of ["downstream", "upstream"]) {
      const cell = textElement("td", row[key]);
      cell.append(textElement("span", "现价 " + (row["system_" + key] ?? "未覆盖"), "old-price"));
      element.append(cell);
    }
    element.append(textElement("td", row.effective_time));
    const labels = [row.merged ? "相同报价已合并" : row.eligible === false ? "阻断，不导出" : "可导出", ...(row.price_increased ? ["涨价"] : []), ...notes(row)];
    element.append(textElement("td", labels.join("；"), "row-notes"));
  });
  if (!rows.length) { const empty = addTableRow(byId("preview-body"), ["没有符合筛选条件的报价。"]); empty.firstChild.colSpan = 7; }
  byId("table-summary").textContent = "符合条件 " + rows.length + " 条；展示前 " + Math.min(100, rows.length) + " 条。CSV 包含完整异常明细，不受此筛选限制。价格币种：" + preview.currency + "。";
}

function renderPreview() {
  const selectedDate = byId("email-date").value;
  const email = preview.email || {};
  const candidates = email.date_candidates || [];
  byId("email-date").replaceChildren(new Option("尚未选择日期", ""), ...candidates.map((item) => new Option(item.label || item.value, item.value)));
  if (candidates.some((item) => item.value === selectedDate)) byId("email-date").value = selectedDate;
  byId("email-review").hidden = !candidates.length && !email.subject && !email.text_excerpt;
  byId("email-summary").textContent = [email.subject, email.text_excerpt, candidates.length ? "提取到 " + candidates.length + " 个候选，需您明确选定。" : "未提取到日期候选，可明确手填。"].filter(Boolean).join(" · ");
  byId("preview-currency").textContent = preview.currency + " · " + (preview.business === "voice" ? "语音演示" : "短信");
  const stats = [["total", "源报价"], ["exported", "可导出"], ["blocked", "阻断"], ["warnings", "提醒"], ["price_increases", "涨价"], ["missing_dates", "原缺日期"], ["invalid_codes", "异常编码"]];
  byId("stats").replaceChildren(...stats.map(([key, label]) => {
    const element = document.createElement("div");
    element.append(textElement("span", label), textElement("strong", count(key), key === "blocked" || key === "price_increases" ? "attention" : ""));
    return element;
  }));
  byId("increase-confirmation").hidden = !count("price_increases");
  byId("partial-confirmation").hidden = !count("blocked");
  const messages = (preview.issues || []).slice(0, 8).map((item) => typeof item === "string" ? item : item.message || item.reason || (item.errors || item.notes || []).join("；")).filter(Boolean);
  byId("issues-summary").textContent = (count("blocked") ? "有 " + count("blocked") + " 行暂时阻断。可选定邮件日期 / 手填日期或修正规则后重新预览；也可明确同意部分导出。" : "") + messages.join("；");
  byId("issues-summary").hidden = !byId("issues-summary").textContent;
  byId("preview-section").hidden = false;
  renderRows();
  setStep(1);
}

async function analyze() {
  if (!selectedFile || configDirty || !supplier()) return;
  invalidate("正在分析报价…");
  const requestRevision = revision;
  const response = await request("/api/workflow/preview", { method: "POST", body: multipart() });
  const data = await response.json();
  if (requestRevision !== revision) return;
  preview = data;
  renderPreview();
  setStatus("预览完成：可导出 " + count("exported") + " 条，阻断 " + count("blocked") + " 条，涨价 " + count("price_increases") + " 条。请核对日期与规则。");
}

function downloadName(response, fallback) {
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const plain = disposition.match(/filename="([^"]+)"|filename=([^;]+)/i);
  let name = fallback;
  try { name = encoded ? decodeURIComponent(encoded[1]) : plain ? (plain[1] || plain[2]).trim() : fallback; } catch {}
  return name.replace(/[\\/\u0000-\u001f]/g, "_");
}
function triggerDownload(url, filename) {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
}
async function exportFile() {
  if (!canExport() || configDirty) return;
  clearResult();
  const requestRevision = revision;
  const data = multipart();
  data.append("confirmed", "true");
  if (count("price_increases")) data.append("confirm_increases", "true");
  if (count("blocked")) data.append("allow_partial", "true");
  const response = await request("/api/workflow/export", { method: "POST", body: data });
  const blob = await response.blob();
  if (requestRevision !== revision) return;
  if (!blob.size || response.headers.get("Content-Type")?.includes("json")) throw new Error("未收到有效的 .xls 文件，请重新导出。");
  exported = { blob, url: URL.createObjectURL(blob), filename: downloadName(response, "影核_目标报价.xls"), proof: response.headers.get("X-Export-Proof") || "",
    supplierId: byId("supplier").value, direction: byId("direction").value,
    increases: Boolean(count("price_increases")), revision, imported: false };
  byId("download-result").href = exported.url;
  byId("download-result").download = exported.filename;
  const amount = response.headers.get("X-Exported-Rows") ?? count("exported");
  const blocked = response.headers.get("X-Skipped-Rows") ?? count("blocked");
  byId("result-counts").textContent = exported.filename + " · 导出 " + amount + " 条，跳过 " + blocked + " 条。异常 CSV 可在预览区下载。";
  byId("result").hidden = false;
  triggerDownload(exported.url, exported.filename);
  setStep(2);
  setStatus("目标 .xls 已生成并开始下载。现在可将此文件导入模拟系统。");
  if (!exported.proof) {
    showError("导出文件缺少绑定凭据，已禁止模拟导入。请重新导出；如仍缺失，请联系维护人员。");
    setStatus("文件已下载，但缺少绑定凭据，无法导入模拟系统。");
  }
}

async function importFile() {
  if (!exported || !exported.proof || exported.imported || exported.revision !== revision) return;
  const file = exported;
  const data = new FormData();
  data.append("file", file.blob, file.filename);
  data.append("supplier_id", file.supplierId);
  data.append("direction", file.direction);
  data.append("confirmed", "true");
  data.append("export_proof", file.proof);
  if (file.increases) data.append("confirm_increases", "true");
  const receipt = await (await request("/api/workflow/import", { method: "POST", body: data })).json();
  if (file !== exported) return;
  if (receipt.mode !== "mock") throw new Error("未收到模拟系统回执，请先核对服务状态。");
  file.imported = true;
  byId("receipt-summary").textContent = receiptText(receipt) + "。已更新当前浏览器的模拟价格。";
  byId("receipt").hidden = false;
  setStatus("模拟导入完成。已取得回执，正在刷新系统价格。");
  await refreshSystem();
  setStatus("模拟导入完成，回执和最新模拟价格已显示。");
}

function csvValue(value) {
  let text = String(value ?? "");
  if (/^[\s\uFEFF]*[=+@-]/u.test(text) || /^[\t\r\n]/u.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}
function downloadIssues() {
  if (!preview) return;
  const records = [["源行", "MCC", "MNC", "下行/呼出", "上行/呼入", "生效时间", "币种", "可导出", "涨价", "异常与提醒"]];
  const issueRows = (preview.rows || []).filter((row) => hasIssue(row) || row.price_increased);
  const includedRows = new Set(issueRows.map((row) => row.source_row));
  issueRows.forEach((row) => records.push([
    row.source_row, row.mcc, row.mnc, row.downstream, row.upstream, row.effective_time,
    row.currency || preview.currency, row.eligible === false ? "否" : "是", row.price_increased ? "是" : "否", notes(row).join("；")
  ]));
  (preview.issues || []).filter((issue) => issue.source_row == null || !includedRows.has(issue.source_row)).forEach((issue) => records.push([
    issue.source_row ?? "", issue.mcc ?? "", issue.mnc ?? "", "", "", "", preview.currency, "", "",
    typeof issue === "string" ? issue : issue.message || issue.reason || (issue.errors || issue.notes || []).join("；")
  ]));
  const blob = new Blob(["\uFEFF" + records.map((row) => row.map(csvValue).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  triggerDownload(url, "影核_完整异常.csv");
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function loadExample(name) {
  invalidate("正在加载模拟示例…", true);
  const preferred = name === "voice" ? "voice-demo" : "standard-sms";
  if (!suppliers.some((item) => item.id === preferred)) throw new Error("未找到该示例对应的供应商配置，请重新加载供应商。");
  byId("supplier").value = preferred;
  populateSupplier();
  byId("direction").value = name === "sms_mt" ? "mt" : name === "sms_mo" ? "mo" : "both";
  selectedFile = null;
  emailFile = null;
  byId("file").value = "";
  byId("email-file").value = "";
  byId("email-text").value = "";
  byId("manual-date").value = "";
  byId("sheet-name").value = "Pricing_";
  byId("filename").textContent = "正在加载示例…";
  byId("email-filename").textContent = "支持 .eml / .txt";
  const response = await request("/api/examples/" + name);
  selectedFile = new File([await response.blob()], downloadName(response, name + ".xls"), { type: "application/vnd.ms-excel" });
  byId("filename").textContent = "模拟示例：" + selectedFile.name;
  const mail = await request("/api/examples/email.eml");
  emailFile = new File([await mail.blob()], "示例邮件.eml", { type: "message/rfc822" });
  byId("email-filename").textContent = "已附示例邮件.eml（可替换或粘贴正文）";
  await analyze();
  await refreshSystem();
}

async function saveSupplier() {
  if (!configForm.reportValidity()) return;
  const fields = Object.fromEntries([...configForm.elements].filter((element) => element.name).map((element) => [element.name, element.value]));
  const headers = Object.fromEntries(headerKeys.map((key) => [key, fields["header_" + key].trim()]));
  const mode = byId("mapping-mode").value;
  if (mode !== "split") { headers.mcc = ""; headers.mnc = ""; }
  if (mode !== "combined") headers.mccmnc = "";
  if (mode !== "country") headers.country = "";
  const data = Object.fromEntries(Object.entries(fields).filter(([key]) => !key.startsWith("header_")));
  try { data.country_map = JSON.parse(data.country_map || "{}"); }
  catch { throw new Error("国家映射字典不是有效 JSON，请检查双引号与逗号。"); }
  if (!data.country_map || typeof data.country_map !== "object" || Array.isArray(data.country_map)) throw new Error("国家映射必须为 JSON 对象，例如 {}。");
  data.headers = headers;
  invalidate("正在保存供应商配置…", true);
  await jsonRequest("/api/suppliers", data);
  await loadSuppliers(data.id);
  await refreshSystem();
  setStatus("供应商配置已保存到当前浏览器工作区，请重新预览报价。");
}

byId("supplier").addEventListener("change", () => {
  invalidate("供应商已切换，旧预览与导出已清空，请重新预览。", true);
  byId("manual-date").value = "";
  byId("baseline-confirmed").checked = false;
  populateSupplier();
  run("正在读取当前供应商模拟数据…", refreshSystem);
});
byId("direction").addEventListener("change", () => invalidate());
byId("sheet-name").addEventListener("input", () => invalidate("工作表名称已变化，请重新预览。", true));
byId("mapping-mode").addEventListener("change", updateMapping);
configForm.addEventListener("input", () => { configDirty = true; invalidate("规则已变化，请先保存配置，再重新预览。", true); });
configForm.addEventListener("change", () => { configDirty = true; invalidate("规则已变化，请先保存配置，再重新预览。", true); });
configForm.addEventListener("submit", (event) => { event.preventDefault(); run("正在保存供应商配置…", saveSupplier); });
byId("file").addEventListener("change", () => {
  invalidate("报价文件已变化，请分析并预览。", true);
  selectedFile = byId("file").files[0] || null;
  if (selectedFile && !/\.xlsx?$/i.test(selectedFile.name)) { selectedFile = null; byId("file").value = ""; showError("请选择 .xls 或 .xlsx 报价文件。"); }
  byId("filename").textContent = selectedFile ? selectedFile.name : "尚未选择报价文件。";
  updateControls();
});
byId("email-file").addEventListener("change", () => {
  invalidate("邮件文件已变化，请重新预览提取候选。", true);
  emailFile = byId("email-file").files[0] || null;
  if (emailFile && !/\.(eml|txt)$/i.test(emailFile.name)) { emailFile = null; byId("email-file").value = ""; showError("请选择 .eml 或 .txt 邮件文件。"); }
  byId("email-filename").textContent = emailFile ? emailFile.name : "支持 .eml / .txt";
});
byId("email-text").addEventListener("input", () => invalidate("邮件正文已变化，请重新预览提取候选。", true));
byId("manual-date").addEventListener("input", () => { byId("email-date").value = ""; invalidate("手填日期已变化，请重新预览。"); });
byId("email-date").addEventListener("change", () => {
  byId("manual-date").value = "";
  invalidate("您已更改邮件日期选择，正在重新预览。");
  run("正在按选定日期重新核价…", analyze);
});
byId("preview").addEventListener("click", () => run("正在核价…", analyze));
byId("export").addEventListener("click", () => run("正在生成目标 .xls…", exportFile));
byId("import").addEventListener("click", () => run("正在上传已导出文件到模拟系统…", importFile));
byId("download-issues").addEventListener("click", downloadIssues);
byId("row-filter").addEventListener("change", renderRows);
byId("download-result").addEventListener("click", (event) => { if (busy || !exported) event.preventDefault(); });
for (const id of ["confirmed", "confirm-increases", "allow-partial"]) byId(id).addEventListener("change", () => { clearResult(); updateControls(); });
document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => run("正在加载模拟示例…", () => loadExample(button.dataset.example))));
byId("load-email").addEventListener("click", () => run("正在加载示例邮件…", async () => {
  invalidate("正在加载示例邮件…", true);
  const response = await request("/api/examples/email.eml");
  emailFile = new File([await response.blob()], "示例邮件.eml", { type: "message/rfc822" });
  byId("email-file").value = "";
  byId("email-text").value = "";
  byId("email-filename").textContent = "已附示例邮件.eml";
  if (selectedFile && !configDirty) await analyze();
  else setStatus("已加载示例邮件，请选择报价后预览日期候选。");
}));
byId("reload-suppliers").addEventListener("click", () => run("正在重新加载供应商…", async () => {
  invalidate("正在重新加载配置…", true);
  await loadSuppliers(); await refreshSystem(); setStatus("供应商配置已加载，请选择报价或一键示例。");
}));
byId("refresh-system").addEventListener("click", () => run("正在刷新模拟系统…", async () => {
  invalidate("系统基准可能变化，请重新预览核价。"); await refreshSystem();
}));
byId("baseline-confirmed").addEventListener("change", updateControls);
byId("reset-confirmed").addEventListener("change", updateControls);
byId("operator-form").addEventListener("input", () => invalidate("模拟编码表单已变化，请重新预览核价。"));
byId("baseline-form").addEventListener("input", (event) => {
  if (event.target.id === "baseline-confirmed") return;
  byId("baseline-confirmed").checked = false;
  invalidate("模拟基准表单已变化，请重新预览核价。");
});
byId("operator-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form));
  run("正在保存模拟合法编码…", async () => {
    invalidate("模拟编码变化，请重新预览。");
    await jsonRequest("/api/mock/operators", data);
    byId("baseline-form").elements.mcc.value = data.mcc;
    byId("baseline-form").elements.mnc.value = data.mnc;
    byId("baseline-confirmed").checked = false;
    await refreshSystem();
    setStatus("合法编码已保存到当前浏览器模拟库。核对第二步表单后，点击保存模拟基准报价。");
  });
});
byId("baseline-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity() || !byId("baseline-confirmed").checked || !supplier()) return;
  const row = Object.fromEntries(new FormData(form));
  row.effective_time = row.effective_time.replace("T", " ");
  if (row.effective_time.length === 16) row.effective_time += ":00";
  if (!row.downstream && !row.upstream) { showError("请至少填写一个方向的模拟基准价。"); return; }
  run("正在保存模拟基准报价…", async () => {
    invalidate("模拟基准变化，请重新预览核价。");
    if (!row.downstream) delete row.downstream;
    if (!row.upstream) delete row.upstream;
    await jsonRequest("/api/mock/rates", { supplier_id: byId("supplier").value, rows: [row], confirmed: true, confirm_increases: true });
    byId("baseline-confirmed").checked = false;
    await refreshSystem(); setStatus("模拟基准报价已保存，仅影响当前浏览器。请重新预览报价以比较涨价。");
  });
});
byId("reset").addEventListener("click", () => {
  if (!byId("reset-confirmed").checked) return;
  run("正在重置当前浏览器的演示工作区…", async () => {
    invalidate("正在重置工作区…", true);
    await jsonRequest("/api/mock/reset", {});
    byId("reset-confirmed").checked = false;
    byId("baseline-confirmed").checked = false;
    await loadSuppliers(); await refreshSystem();
    setStatus("当前浏览器的模拟工作区已重置为样例；其他浏览器不受影响。请重新预览。");
  });
});
window.addEventListener("pagehide", () => { if (exported) URL.revokeObjectURL(exported.url); });
run("正在加载当前浏览器演示工作区…", async () => {
  await loadSuppliers(); await refreshSystem(); setStatus("准备就绪，请上传报价或点击一键示例。邮件日期提取后仍需您明确选定。");
});
