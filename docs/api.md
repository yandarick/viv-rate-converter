# 接口指南

[返回 README](../README.md) · [系统指南](system-guide.md)

以下为仓库当前实现的本地演示接口，不是真实 Viv API 契约。基础地址示例为 `http://127.0.0.1:18041`，所有费率、编码和回执均来自模拟工作区。没有大模型、真实邮箱或内部系统调用。

## 1. 会话与数据约定

- 首次访问工作区接口时，服务用签名会话 Cookie 保存工作区标识。页面同源调用自动携带 Cookie；脚本需复用同一个 HTTP 会话。不要发送自定义工作区ID来切换别人的数据。
- Cookie 不是账号或 API Key；接口没有内置用户认证。未配置跨源开放，也没有独立业务审批令牌。对外服务的访问控制须由部署者补充。
- JSON请求用 `Content-Type: application/json`，顶层为对象；文件接口用 `multipart/form-data`，让客户端库自行生成边界。
- **JSON确认字段用布尔值 `true`，表单确认字段用字符串 `"true"`**；`"false"`、`1` 等不等价。
- 费率建议用十进制字符串；编码必须用文本，保留 `"00"` 等前导零。JSON导入日期必须已经转换为 GMT+8，并使用 `YYYY-MM-DD HH:MM:SS`，接口不会替你转换。
- 常规模拟JSON成功响应含 `mode: "mock"` 和 `notice`；预览含 `mode`，无 `notice`。文件响应是二进制，健康检查使用 `system_mode`。错误格式见末节。
- `business` 为 `sms` 或 `voice`；省略时从供应商取值，传入则必须匹配。方向为 `mt`、`mo`、`both`。

## 2. 接口总表

| 方法与路径 | 输入/成功响应 |
| --- | --- |
| `GET /health` | `{status, service, version, system_mode}`；版本为 `2026.11.12` |
| `GET /api/suppliers` | `{mode, notice, suppliers: [...]}` |
| `POST /api/suppliers` | 供应商配置JSON；返回 `{mode, notice, supplier}` |
| `GET /api/mock/operators?business=sms` | 默认 `sms`；返回 `{mode, notice, operators: [...]}` |
| `POST /api/mock/operators` | 编码JSON；返回 `{mode, notice, operator}` |
| `GET /api/mock/rates?supplier_id=standard-sms` | 可带 `business`；返回 `{mode, notice, supplier_id, business, timezone, rates}` |
| `POST /api/mock/query` | 批量查询JSON；返回同上，`rates` 为按 `mcc:mnc` 索引的对象 |
| `POST /api/mock/rates` | 基准维护JSON，必须 `confirmed: true`；返回导入回执 |
| `POST /api/mock/import` | 规范费率JSON；返回导入回执，不需要文件证明 |
| `POST /api/workflow/preview` | 报价/邮件/规则表单；返回预览JSON |
| `POST /api/workflow/export` | 同上加确认；返回 `.xls` 和导出证明头 |
| `POST /api/workflow/import` | 导出的 `.xls`、证明与确认表单；返回导入回执 |
| `GET /api/mock/receipts` | `{mode, notice, receipts: [...]}`，按最新写入在前 |
| `POST /api/mock/reset` | 重置当前工作区；返回 `{mode, notice, reset: true}` |
| `GET /api/examples/<name>` | `sms_mt`、`sms_mo`、`sms_both`、`voice`；返回动态合成 `.xls` |
| `GET /api/examples/email.eml` | 返回带单一日期的合成邮件 |
| `POST /api/analyze`、`POST /api/convert` | 固定结构旧接口，见第7节 |

这些成功响应当前均为 HTTP 200，不使用202任务轮询或201创建响应。供应商、编码、费率接口按工作区隔离；首次使用会初始化合成种子数据。

## 3. 供应商、编码与基准查询

### 保存供应商

`POST /api/suppliers` 按 `id` 保存或覆盖配置。必填 `id`、`name`；没有传入的可选字段按默认值补齐，**不是仅更新差异字段**，修改已有配置时建议先查询再提交完整对象。

| 字段 | 类型、默认值与约束 |
| --- | --- |
| `id` | 文本，1至64字符，字母/数字开头，只含字母、数字、`_`、`-` |
| `name` | 非空文本，最多120字符 |
| `business`、`timezone` | 默认 `sms`、`UTC`；时区须为有效 IANA 时区，例如 `Asia/Shanghai` |
| `currency` | 默认 `EUR`；支持 EUR、USD、GBP、CNY、HKD、SGD、CAD、AUD、JPY、CHF |
| `divisor` | 字符串 `"1"`（默认）或 `"100"` |
| `headers` | 对象，可含 `mcc/mnc/mt/mo/date/mccmnc/country`；值为源表头，空字符串表示不映射 |
| `sheet_name` | 可选，最长31字符；多表工作簿须指定。表单可覆盖本次选择 |
| `missing_date_policy` | `email_then_system`（默认）、`manual`、`same_file`、`skip` |
| `invalid_code_policy` | `max`（默认）、`contract`、`skip` |
| `fallback_mnc` | 目前只能为字符串 `"999"`，仅模拟映射 |
| `contract_rate`、`contract_currency` | 默认 `"5"`、`USD`；只在 `contract` 策略使用 |
| `contract_fx_rate` | 默认空字符串；跨币种时必须为正数乘数 |
| `country_map` | 可选，最多500项：`{"来源标识":{"mcc":"730","mnc":"999"}}`；必须人工明确 |

默认表头为 `MCC`、`MNC`、`New Price`、`MO`、`Effective Date`。至少配置 MCC、合并码或国家标识之一，至少配置一个价格方向；映射名不可重复。请求不接受未知字段、内部系统地址或凭据。有基准价的供应商不能直接更换业务或币种，需新建供应商或有意重置工作区。

例如新增一个只使用合成数据的下行配置：

```json
{
  "id": "sample-sms",
  "name": "合成报价演示",
  "business": "sms",
  "timezone": "UTC",
  "currency": "EUR",
  "divisor": "1",
  "headers": {"mcc": "MCC", "mnc": "MNC", "mt": "New Price", "mo": "", "date": "Effective Date"},
  "missing_date_policy": "manual",
  "invalid_code_policy": "skip"
}
```

`POST /api/mock/operators` 必须恰含 `business`、`mcc`、`mnc`、`name` 四项，例如 `{"business":"sms","mcc":"460","mnc":"00","name":"模拟编码"}`。不会据此生成任何真实运营商资质。

批量查询请求示例：

```json
{"supplier_id":"standard-sms","business":"sms","keys":["460:00","234:10"]}
```

`keys` 必填，最多20,000个合法格式的文本键；未知基准不出现在 `rates` 中，不能当作0。查询还接受可选 `direction` 并校验其取值，但不按方向裁剪结果。每项费率响应包含 `downstream`、`upstream`、`effective_time`、`currency`、`timezone: "Asia/Shanghai"`。

## 4. 报价预览、导出和文件导入

### 预览与导出表单

| 字段 | 必填/行为 |
| --- | --- |
| `supplier_id` | 必填，当前工作区已保存的供应商ID |
| `file` | 必填，源报价 `.xls/.xlsx` 文件 |
| `direction` | 可选，后端默认 `mt`；页面默认 `both`，脚本建议显式指定 |
| `sheet_name` | 可选，本次工作表名称，覆盖供应商设置 |
| `email_text` | 可选，正文文本 |
| `email_file` | 可选，`.eml` 或 UTF-8 `.txt`；与正文合并，受512 KiB限制 |
| `email_date` | 可选，必须精确等于本次正文返回的某个 `date_candidates[].value` |
| `manual_date` | 可选，明确填写的补空日期，可带 UTC偏移；不能覆盖原表已有非法日期 |
| `confirmed` | 导出必填字符串 `"true"`，确认规则和模拟来源 |
| `confirm_increases` | 导出有可处理涨价行时须为字符串 `"true"` |
| `allow_partial` | 导出存在阻断行时须为字符串 `"true"`，明确仅导出可处理行 |

`POST /api/workflow/preview` 返回：

- 上下文：`supplier`（实际配置）、`filename`、`sheet`、`columns`、`business`、`direction`、`currency`、`mode`。
- `stats`：`total`、`exported`（可导出数，尚未真正导出）、`blocked`、`warnings`、`price_increases`、`missing_dates`、`invalid_codes`、`merged`。`warnings` 按有说明的行统计，合并行不算阻断行。
- `rows`：全部原始处理行，含 `source_row/mcc/mnc/raw_mnc/currency`、`downstream/upstream/effective_time`、`date_source`、`system_downstream/system_upstream/system_effective_time`、`notes/errors`、`price_increased/eligible/merged`。
- `issues`：有 `errors` 或 `notes` 的行；不是仅错误清单。
- `email`：`subject`、前1200字符的 `text_excerpt`、候选数组 `date_candidates`，候选含 `value/label/context`。

行级错误通常仍返回200，须检查 `stats.blocked` 和每行 `errors`；文件级错误才返回400。预览不写入费率。导出重新执行同样解析，不接受客户端直接提交预览结果替代源文件。

`POST /api/workflow/export` 返回 MIME `application/vnd.ms-excel` 的真实 `.xls`，同时返回：

| 响应头 | 含义 |
| --- | --- |
| `Content-Disposition` | 下载文件名 |
| `X-Exported-Rows` | 实际输出行数 |
| `X-Skipped-Rows` | 阻断行数；不包含已合并的重复行 |
| `X-Price-Increases` | 可输出的涨价行数 |
| `X-Export-Proof` | 1小时有效的签名证明；调用者须保存，不能从文件名推导 |

### 文件导入表单

`POST /api/workflow/import` 接受 `supplier_id`、`file`（刚导出的 `.xls`）、`export_proof`、`confirmed="true"`，存在涨价还需 `confirm_increases="true"`。前端也发送 `direction`，但此路由不按该字段重做方向转换；实际导入文件中的两个费率列。

服务核对文件摘要、工作区、供应商、币种、业务、证明时效，以及 `Sheet1` 五列表头、编码文本类型、Excel日期类型。文件内容被修改、跨浏览器/供应商或证明过期，都需要重新导出。证明不绑定所有规则字段、不提供一次性消费限制；规范JSON导入也不要求它，不能将其理解为完整权限控制。

文件校验通过后调用同一事务导入逻辑。回执直接包含 `receipt_id/imported/submitted/deduplicated/supplier_id/business/currency/timezone/mode/notice/increases/confirm_increases/created_at/created_at_timezone`。费率生效时间为 GMT+8；回执创建时间明确为 UTC。导入失败整体回滚，不部分落库；文件没有幂等业务键，重复提交可产生新回执。

## 5. 规范JSON导入与基准维护

`POST /api/mock/import` 接受以下顶层字段：`supplier_id`、可选 `business/direction`、必填 `rows`、可选布尔值 `confirm_increases`（默认false）。也接受 `confirmed`，但此接口当前不强制检查它。`POST /api/mock/rates` 使用同一结构，额外**必须** `confirmed: true`。

```json
{
  "supplier_id": "standard-sms",
  "business": "sms",
  "direction": "both",
  "confirm_increases": true,
  "rows": [
    {"mcc":"460","mnc":"00","downstream":"0.015","upstream":"0.003","effective_time":"2026-11-15 17:00:00","currency":"EUR"}
  ]
}
```

以上为合成演示请求。`rows` 为1至20,000项数组；行只允许 `mcc/mnc/downstream/upstream/effective_time/currency/supplier_id/business`。编码、生效日期、币种必填；至少给一个方向价格，新增基准必须给齐两方向。行可选供应商、业务必须匹配顶层；币种必须与供应商一致，不自动换汇。

若显式传 `direction=mt/mo`，行必须且只能带对应费率，另一方向从当前基准保留；`both` 必须齐带两方向。省略方向时允许逐行提供一个或两个方向。这与文件流程 `mt` 输出上行0的行为不同。所有目标编码必须在当前业务字典内；完全一致重复行合并，冲突重复行拒绝；事务内重读最新基准，未确认涨价则整体拒绝。成功返回与文件导入相同的模拟回执。

`POST /api/mock/reset` 当前没有服务端确认字段校验；页面勾选是前端保护，脚本调用须自行确认意图。它清除当前工作区配置、费率和回执并恢复种子，不能撤销真实系统业务。

## 6. 最小可复现模拟流程

先按 README 启动专用本地实例，另安装脚本用依赖 `python -m pip install requests`。下面 Python 示例只使用接口动态生成的合成文件；整个过程复用一个会话。它检查合成邮件仅有一个日期才继续，真实多候选须人工选择。示例明确确认演示涨价和部分导出，不应用于未经审核的真实报价：

```python
from pathlib import Path
import requests

base = 'http://127.0.0.1:18041'
session = requests.Session()

def checked(response):
    response.raise_for_status()
    return response

suppliers = checked(session.get(base + '/api/suppliers', timeout=20)).json()
assert suppliers['mode'] == 'mock'
source = checked(session.get(base + '/api/examples/sms_both', timeout=20)).content
mail = checked(session.get(base + '/api/examples/email.eml', timeout=20)).content
options = {'supplier_id': 'standard-sms', 'direction': 'both'}
files = {'file': ('demo.xls', source), 'email_file': ('demo.eml', mail)}
preview = checked(session.post(base + '/api/workflow/preview', data=options, files=files, timeout=60)).json()
print('首次预览：', preview['stats'])
assert len(preview['email']['date_candidates']) == 1
options['email_date'] = preview['email']['date_candidates'][0]['value']
preview = checked(session.post(base + '/api/workflow/preview', data=options, files=files, timeout=60)).json()
print('日期确认后：', preview['stats'])
options.update(confirmed='true', confirm_increases='true', allow_partial='true')
exported = checked(session.post(base + '/api/workflow/export', data=options, files=files, timeout=60))
proof = exported.headers['X-Export-Proof']
output = Path('.agent/tmp/api-demo')
output.mkdir(parents=True, exist_ok=True)
(output / 'result.xls').write_bytes(exported.content)
receipt = checked(session.post(
    base + '/api/workflow/import',
    data={'supplier_id': 'standard-sms', 'confirmed': 'true',
          'confirm_increases': 'true', 'export_proof': proof},
    files={'file': ('result.xls', exported.content)}, timeout=60,
)).json()
print('模拟回执：', receipt)
query = checked(session.post(base + '/api/mock/query', json={
    'supplier_id': 'standard-sms', 'keys': ['460:00'],
}, timeout=20)).json()
print('模拟最新价格：', query['rates'])
```

示例返回数量取决于当前工作区配置；应在未修改的种子环境复现。重新提交同一报价可检查涨价标记变化。不要仅凭脚本成功或模拟回执宣称真实导入成功。本次文档更新未运行这段示例；仓库已有完整检查命令见[系统指南](system-guide.md)。

## 7. 旧兼容接口

旧接口只接受表单文件 `file`，文件名须为 `.xlsx`；固定识别 `MCC`、`MNC`、`New Price (EUR/100)`、`Price Valid from`，不使用供应商配置、邮件时区或模拟基准。

- `/api/analyze` 返回 `filename/sheet/source_unit/columns/preview/issues/stats`；`preview` 最多12行，统计含 `total_rows/missing_dates/valid_rows/invalid_rows/duplicate_rows`。
- `/api/convert` 额外要求 `confirmed="true"`、`target_unit=per100|per1`、`upstream_rate`、`missing_date_policy=skip|fill|keep`、`duplicate_policy=skip|keep`；`fill` 还要求 `fallback_date`。`per1` 对原新价除100，`per100` 保留原值，旧日期不接受时区信息。
- 成功下载 `Template_sendRate_converted.xls`，附 `X-Exported-Rows/X-Skipped-Rows/X-Warning-Rows/X-Duplicate-Rows`。不生成 `X-Export-Proof`，不能直接走新文件证明导入流程。

旧接口保留自身的缺失日期/重复策略，不能套用新工作流的规则；新增业务应以 `/api/workflow/*` 为入口。

## 8. 错误、重试与排查

| 情况 | 实际响应/处理 |
| --- | --- |
| 输入、规则、确认或证明不合法 | 通常400；`{"error":"中文原因"}`。`demo_api.py` 蓝图中的 ValueError 还含 `mode/notice`，不能要求所有错误都有它们 |
| 预览内行级阻断 | 仍为200，检查行 `errors`、`eligible` 和 `stats.blocked`，不能只看HTTP状态 |
| 请求超限 | 413，应用JSON消息为“上传文件超过10MB限制”；表单分段/内存阈值也可能触发413，需检查具体请求 |
| JSON无效/过大 | 通常400；顶层须对象且不超过8 MiB，不接受未知顶层字段的路由会拒绝扩展字段 |
| 路径不存在/方法错误 | 404/405，应用一般返回 `{"error":"请求无法处理"}` |
| 网关限流/网关超时 | 可能429/502/504，网关不保证返回应用JSON；先检查状态与Content-Type |
| 未捕获异常 | 500，`{"error":"文件处理失败，请检查文件格式或稍后重试"}`，服务端记录异常 |

所有应用响应设置禁止缓存；模拟蓝图另带 `X-Viv-Mode: mock`，不要期待每个工作流响应都有该头。出现证明过期或文件不匹配，应在原会话重新预览导出；基准变化导致新的涨价阻断，应重新复核。收到导入超时或连接中断时，先查询回执与价格再决定是否重试，因为当前接口没有幂等键。不要用重置工作区绕过需要保留数据的错误。
