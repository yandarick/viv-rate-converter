from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from io import BytesIO
import hashlib

import xlrd
import xlwt
from flask import Blueprint, current_app, jsonify, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from converter import decimal_value
from mail_dates import mail_context, parse_time
from quote_reader import read_quotes, xls_sheets


workflow_api = Blueprint('workflow', __name__)
SMS_HEADERS = ['MCC', 'MNC', '下行费率', '上行费率', '生效时间']
VOICE_HEADERS = ['CC', 'MNC', '呼出费率', '呼入费率', '生效时间']


def mock_context():
    from demo_api import get_store, workspace_id
    return get_store(), workspace_id()


def get_profile(store, workspace, supplier_id):
    supplier = store.get_supplier(workspace, supplier_id)
    if not supplier:
        raise ValueError('请选择已配置的供应商')
    return supplier


def stamp(value):
    return value.isoformat(sep=' ', timespec='seconds') if value else ''


def build_preview(payload, filename, supplier, direction, options, store, workspace, mail=None):
    if direction not in ('mt', 'mo', 'both'):
        raise ValueError('请选择报价方向')
    mail = mail or mail_context()
    email_date = options.get('email_date', '').strip()
    if email_date and email_date not in {candidate['value'] for candidate in mail['date_candidates']}:
        raise ValueError('所选邮件日期不在候选列表中，请重新选择或使用手动日期')
    selected_email = parse_time(email_date, supplier['timezone']) if email_date else None
    manual_date = parse_time(options.get('manual_date'), supplier['timezone'])
    sheet_name, columns, source_rows = read_quotes(payload, filename, supplier, direction)
    operators = store.operators(workspace, supplier['business'])
    known = {f'{item["mcc"]}:{item["mnc"]}' for item in operators}
    keys = {f'{row["mcc"]}:{row["mnc"]}' for row in source_rows if f'{row["mcc"]}:{row["mnc"]}' in known}
    keys.update(f'{row["mcc"]}:{supplier["fallback_mnc"]}' for row in source_rows if f'{row["mcc"]}:{supplier["fallback_mnc"]}' in known)
    system_rates = store.query_rates(workspace, supplier['id'], supplier['business'], sorted(keys))
    divisor = decimal_value(supplier['divisor'], '价格除数')
    prepared = []
    file_dates = set()
    for source in source_rows:
        row = dict(source) | {'notes': [], 'effective_time': None, 'currency': supplier['currency'], 'date_source': '', 'invalid_code': False}
        row['errors'] = list(source['errors'])
        if source.get('country_mapping'):
            row['notes'].append('国家编码按人工配置映射：' + source['country_mapping'])
        for field in ('mt', 'mo'):
            row[field] = source[field] / divisor if source[field] is not None else None
        if row['date'] is not None:
            try:
                row['effective_time'] = parse_time(row['date'], supplier['timezone'])
                row['date_source'] = '报价表；供应商时区转GMT+8'
                file_dates.add(row['effective_time'])
            except ValueError as error:
                row['errors'].append(str(error))
        prepared.append(row)
    missing_dates = sum(row['date'] is None for row in prepared)
    for row in prepared:
        source_key = f'{row["mcc"]}:{row["mnc"]}'
        invalid = source_key not in known or bool(row['code_issue'])
        row['invalid_code'] = invalid
        if invalid and row['mcc']:
            if supplier['invalid_code_policy'] == 'skip':
                row['errors'].append('编码不在模拟系统字典中；当前规则为排除')
            else:
                row['mnc'] = supplier['fallback_mnc']
                if f'{row["mcc"]}:{row["mnc"]}' not in known:
                    row['errors'].append('模拟编码库未覆盖该MCC及约定替代MNC，请先维护编码库')
                row['notes'].append(f'原MNC {row["raw_mnc"] or "空"} 无效，按演示配置映射为 {row["mnc"]}；非真实系统约定')
        row['system'] = system_rates.get(f'{row["mcc"]}:{row["mnc"]}')
        if direction == 'mo':
            if row['system']:
                row['mt'] = Decimal(row['system']['downstream'])
                row['notes'].append('单独上行报价，另一方向沿用模拟系统已有费率')
            else:
                row['errors'].append('单独上行缺少另一方向基准，不能默认把下行费率清零；请补模拟系统基准或提供双向报价')
        if row['date'] is None:
            policy = supplier['missing_date_policy']
            if policy == 'skip':
                row['errors'].append('缺少生效日期；当前规则为排除')
            elif selected_email:
                row['effective_time'], row['date_source'] = selected_email, '用户确认的邮件日期；转换为GMT+8'
            elif manual_date:
                row['effective_time'], row['date_source'] = manual_date, '用户填写日期；供应商时区转GMT+8'
            elif mail['date_candidates']:
                row['errors'].append('邮件包含日期候选，请先确认本次报价生效日期')
            elif policy == 'same_file' and len(file_dates) == 1:
                row['effective_time'], row['date_source'] = next(iter(file_dates)), '同表唯一非空日期（已选择此规则）'
            elif policy == 'email_then_system' and row['system'] and row['system'].get('effective_time'):
                row['effective_time'] = parse_time(row['system']['effective_time'], 'Asia/Shanghai')
                row['date_source'] = '模拟系统已有日期（按已选规则沿用，不代表真实系统业务批准）'
            else:
                row['errors'].append('缺少可用生效日期，请提供邮件日期或手动日期')
            if row['effective_time']:
                row['notes'].append('原日期为空；' + row['date_source'])
    maxima = defaultdict(lambda: {'mt': None, 'mo': None})
    for row in prepared:
        if not row['mcc'] or row['errors']:
            continue
        for side in ('mt', 'mo'):
            value = row[side]
            previous = maxima[row['mcc']][side]
            if value is not None and (previous is None or value > previous):
                maxima[row['mcc']][side] = value
    for row in prepared:
        if row['invalid_code'] and row['mcc'] and not row['errors']:
            if supplier['invalid_code_policy'] == 'max':
                for side in ('mt', 'mo'):
                    if direction != 'both' and side != direction:
                        continue
                    row[side] = maxima[row['mcc']][side]
                    if row[side] is None:
                        row['errors'].append('同MCC没有可用的最高费率')
                row['notes'].append('按本供应商当前文件同MCC分别取上下行最高价；仅比较可处理行')
            elif supplier['invalid_code_policy'] == 'contract':
                rate = decimal_value(supplier['contract_rate'], '约定单价')
                if supplier['contract_currency'] != supplier['currency']:
                    if not supplier.get('contract_fx_rate'):
                        row['errors'].append('约定单价币种不同，需明确填写约定币种到报价币种的换算系数；系统不猜汇率')
                    else:
                        rate *= decimal_value(supplier['contract_fx_rate'], '约定价换算系数')
                for side in ('mt', 'mo'):
                    if direction in (side, 'both'):
                        row[side] = rate
                row['notes'].append(f'使用约定单价 {supplier["contract_rate"]} {supplier["contract_currency"]}；输出币种 {supplier["currency"]}')
        row['price_increased'] = False
        if row['system']:
            if row['system']['currency'] != supplier['currency']:
                row['errors'].append('模拟系统基准币种与供应商币种不同，不能直接比较或覆盖')
            else:
                row['price_increased'] = any(row[side] is not None and row[side] > Decimal(row['system'][field]) for side, field in [('mt', 'downstream'), ('mo', 'upstream')])
                if row['price_increased']:
                    row['notes'].append('较模拟系统已有报价上涨，导出与导入均须确认')
        else:
            row['notes'].append('模拟系统无该供应商编码的已有报价，无法判断涨跌')
        for side in ('mt', 'mo'):
            if row[side] is not None:
                try:
                    row[side] = decimal_value(row[side], '输出费率')
                except ValueError as error:
                    row['errors'].append(str(error))
    groups = defaultdict(list)
    for row in prepared:
        if not row['errors']:
            groups[f'{row["mcc"]}:{row["mnc"]}'].append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        values = {(row['mt'], row['mo'], row['effective_time']) for row in group}
        if len(values) == 1:
            group[0]['notes'].append(f'同编码、同日期、同报价的{len(group)}条记录合并为一条')
            for row in group[1:]:
                row['merged'] = True
                row['notes'].append('与另一行结果完全一致，已合并输出')
        else:
            for row in group:
                row['errors'].append('同一目标编码出现不同报价或日期；需人工处理，未自动覆盖')
    rows = []
    for row in prepared:
        system = row['system'] or {}
        rows.append({key: row[key] for key in ('source_row', 'mcc', 'mnc', 'currency', 'notes', 'errors', 'price_increased', 'date_source', 'raw_mnc')} | {
            'downstream': str(row['mt']) if row['mt'] is not None else '',
            'upstream': str(row['mo']) if row['mo'] is not None else '',
            'effective_time': stamp(row['effective_time']),
            'system_downstream': system.get('downstream', ''), 'system_upstream': system.get('upstream', ''),
            'system_effective_time': system.get('effective_time', ''),
            'eligible': not row['errors'] and not row.get('merged', False), 'merged': row.get('merged', False),
        })
    stats = {'total': len(rows), 'exported': sum(row['eligible'] for row in rows), 'blocked': sum(bool(row['errors']) for row in rows),
             'warnings': sum(bool(row['notes']) for row in rows), 'price_increases': sum(row['eligible'] and row['price_increased'] for row in rows),
             'missing_dates': missing_dates, 'invalid_codes': sum(row['invalid_code'] for row in prepared), 'merged': sum(row['merged'] for row in rows)}
    return {'supplier': supplier, 'filename': filename, 'sheet': sheet_name, 'columns': columns, 'business': supplier['business'], 'direction': direction,
            'currency': supplier['currency'], 'stats': stats, 'rows': rows, 'issues': [row for row in rows if row['errors'] or row['notes']], 'email': mail, 'mode': 'mock'}


def export_preview(preview, options):
    if options.get('confirmed') != 'true':
        raise ValueError('请先确认本次使用的供应商规则与模拟数据来源')
    if preview['stats']['price_increases'] and options.get('confirm_increases') != 'true':
        raise ValueError('有报价上涨，请核对并明确确认涨价后再导出')
    if preview['stats']['blocked'] and options.get('allow_partial') != 'true':
        raise ValueError('存在阻断行，请处理后重试，或明确同意只导出可处理行')
    if not preview['stats']['exported']:
        raise ValueError('没有可导出的报价，请先处理异常或补充日期')
    workbook = xlwt.Workbook(encoding='utf-8')
    sheet = workbook.add_sheet('Sheet1')
    header = xlwt.easyxf('font: bold on; pattern: pattern solid, fore_colour ice_blue;')
    rate = xlwt.easyxf(num_format_str='0.##############')
    rise = xlwt.easyxf('pattern: pattern solid, fore_colour light_yellow;', num_format_str='0.##############')
    date = xlwt.easyxf(num_format_str='yyyy-mm-dd hh:mm:ss')
    headers = VOICE_HEADERS if preview['business'] == 'voice' else SMS_HEADERS
    for column, value in enumerate(headers):
        sheet.write(0, column, value, header)
        sheet.col(column).width = 6500 if column == 4 else 4200
    for output_row, row in enumerate((row for row in preview['rows'] if row['eligible']), 1):
        sheet.write(output_row, 0, row['mcc'])
        sheet.write(output_row, 1, row['mnc'])
        for column, field in [(2, 'downstream'), (3, 'upstream')]:
            value = decimal_value(row[field], '输出费率')
            sheet.write(output_row, column, float(value), rise if row['price_increased'] else rate)
        sheet.write(output_row, 4, datetime.fromisoformat(row['effective_time']), date)
    sheet.set_panes_frozen(True)
    sheet.set_horz_split_pos(1)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def request_preview():
    store, workspace = mock_context()
    supplier = get_profile(store, workspace, request.form.get('supplier_id'))
    if request.form.get('sheet_name'):
        supplier = dict(supplier) | {'sheet_name': request.form['sheet_name'][:80]}
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        raise ValueError('请选择供应商Excel报价文件')
    email_file = request.files.get('email_file')
    context = mail_context(request.form.get('email_text', ''), email_file.read() if email_file and email_file.filename else None, email_file.filename if email_file else '')
    return build_preview(upload.read(), upload.filename[:180], supplier, request.form.get('direction', 'mt'), request.form, store, workspace, context)


@workflow_api.post('/api/workflow/preview')
def preview_api():
    return jsonify(request_preview())


@workflow_api.post('/api/workflow/export')
def export_api():
    preview = request_preview()
    output = export_preview(preview, request.form)
    filename = ('Voice_demo' if preview['business'] == 'voice' else 'Template_sendRate') + '_' + preview['currency'] + '.xls'
    response = send_file(BytesIO(output), mimetype='application/vnd.ms-excel', as_attachment=True, download_name=filename, max_age=0)
    response.headers.update({'X-Exported-Rows': str(preview['stats']['exported']), 'X-Skipped-Rows': str(preview['stats']['blocked']), 'X-Price-Increases': str(preview['stats']['price_increases'])})
    _, workspace = mock_context()
    proof = {'sha256': hashlib.sha256(output).hexdigest(), 'workspace': workspace, 'supplier_id': preview['supplier']['id'],
             'currency': preview['currency'], 'business': preview['business']}
    response.headers['X-Export-Proof'] = URLSafeTimedSerializer(current_app.secret_key, salt='viv-export-v1').dumps(proof)
    return response


@workflow_api.post('/api/workflow/import')
def import_api():
    if request.form.get('confirmed') != 'true':
        raise ValueError('请确认将文件导入当前浏览器的模拟系统')
    store, workspace = mock_context()
    supplier = get_profile(store, workspace, request.form.get('supplier_id'))
    upload = request.files.get('file')
    if upload is None or not upload.filename or not upload.filename.lower().endswith('.xls'):
        raise ValueError('请选择本工具生成的真实xls文件')
    payload = upload.read()
    if not payload or len(payload) > 10 * 1024 * 1024:
        raise ValueError('导入文件为空或超出10MB')
    try:
        proof = URLSafeTimedSerializer(current_app.secret_key, salt='viv-export-v1').loads(request.form.get('export_proof', ''), max_age=3600)
    except (BadSignature, SignatureExpired):
        raise ValueError('导出证明缺失或已过期，请在本浏览器重新生成文件后导入') from None
    expected = {'sha256': hashlib.sha256(payload).hexdigest(), 'workspace': workspace, 'supplier_id': supplier['id'],
                'currency': supplier['currency'], 'business': supplier['business']}
    if proof != expected:
        raise ValueError('文件、供应商、币种或浏览器工作区已变化，请重新生成结果后导入')
    list(xls_sheets(payload))
    book = xlrd.open_workbook(file_contents=payload)
    headers = VOICE_HEADERS if supplier['business'] == 'voice' else SMS_HEADERS
    sheet = book.sheet_by_index(0)
    if sheet.name != 'Sheet1' or sheet.ncols != 5 or sheet.row_values(0) != headers:
        raise ValueError('导入文件不是当前业务的标准五列模板')
    rows = []
    for index in range(1, sheet.nrows):
        if sheet.cell_type(index, 4) != xlrd.XL_CELL_DATE:
            raise ValueError(f'第{index+1}行生效时间不是有效Excel日期')
        if not all(sheet.cell_type(index, column) == xlrd.XL_CELL_TEXT for column in (0, 1)):
            raise ValueError('编码必须按文本保存，避免前导零丢失')
        rows.append({'mcc': sheet.cell_value(index, 0), 'mnc': sheet.cell_value(index, 1), 'downstream': str(sheet.cell_value(index, 2)),
                     'upstream': str(sheet.cell_value(index, 3)), 'effective_time': stamp(xlrd.xldate_as_datetime(sheet.cell_value(index, 4), book.datemode)), 'currency': supplier['currency']})
    result = store.import_rows(workspace, supplier['id'], supplier['business'], rows, confirm_increases=request.form.get('confirm_increases') == 'true')
    return jsonify(result)


def register_workflow(app):
    app.register_blueprint(workflow_api)
