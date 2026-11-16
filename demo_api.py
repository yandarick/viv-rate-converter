from io import BytesIO
import os
from pathlib import Path
import secrets
import tempfile
from uuid import UUID, uuid4

from flask import Blueprint, current_app, jsonify, request, send_file, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
import xlwt

from mock_store import MAX_JSON_BYTES, MOCK_NOTICE, MockStore, json_size


demo = Blueprint('viv_demo', __name__)


class ProtocolSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure


def get_store():
    return current_app.extensions['viv_mock_store']


def workspace_id():
    identifier = session.get('viv_workspace_id')
    try:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError('invalid workspace')
    except (ValueError, TypeError, AttributeError):
        identifier = str(uuid4())
        session['viv_workspace_id'] = identifier
    return identifier


def persistent_secret(data_dir):
    secret_path = data_dir / '.session-key'
    if not secret_path.exists():
        descriptor, temporary = tempfile.mkstemp(prefix='.session-key-', dir=data_dir)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_hex(32))
            try:
                os.link(temporary, secret_path)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    value = secret_path.read_text(encoding='utf-8').strip()
    if len(value) < 32 or len(value) > 4096:
        raise ValueError('持久化会话密钥文件无效')
    return value


def register_demo(app):
    if 'viv_mock_store' in app.extensions:
        return app.extensions['viv_mock_store']
    data_dir = Path(app.config.get('VIV_DATA_DIR') or os.environ.get('VIV_DATA_DIR') or Path(app.root_path) / '.runtime')
    store = MockStore(data_dir)
    app.secret_key = os.environ.get('VIV_SECRET_KEY') or os.environ.get('SECRET_KEY') or app.secret_key or persistent_secret(data_dir)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    app.session_interface = ProtocolSessionInterface()
    if os.environ.get('VIV_SECURE_COOKIE') == '1' or app.config.get('VIV_SECURE_COOKIE'):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    app.extensions['viv_mock_store'] = store
    app.register_blueprint(demo)
    return store


def response(**values):
    return jsonify(mode='mock', notice=MOCK_NOTICE, **values)


def body(allowed=None):
    if not request.is_json:
        raise ValueError('当前接口仅接受application/json规范数据')
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise ValueError('JSON请求超过8MB限制')
    try:
        raw = request.stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError('JSON请求超过8MB限制')
        data = current_app.json.loads(raw)
    except (RequestEntityTooLarge, ValueError, UnicodeError, RecursionError):
        raise ValueError('JSON请求无效或超过大小限制') from None
    if not isinstance(data, dict):
        raise ValueError('JSON请求必须是对象')
    json_size(data)
    if allowed is not None and set(data) - set(allowed):
        raise ValueError('请求包含不支持的字段')
    return data


def supplier_context(data):
    identifier = data.get('supplier_id')
    supplier = get_store().get_supplier(workspace_id(), identifier)
    business = data.get('business', supplier['business'])
    if business != supplier['business']:
        raise ValueError('业务类型与当前供应商不匹配')
    direction = data.get('direction')
    if direction is not None and (not isinstance(direction, str) or direction not in {'mt', 'mo', 'both'}):
        raise ValueError('方向必须是mt、mo或both')
    return identifier, business


@demo.errorhandler(ValueError)
def invalid_demo_input(error):
    return jsonify(mode='mock', notice=MOCK_NOTICE, error=str(error)), 400


@demo.after_request
def mock_headers(result):
    result.headers['X-Viv-Mode'] = 'mock'
    result.headers['Cache-Control'] = 'no-store'
    result.headers['X-Content-Type-Options'] = 'nosniff'
    return result


@demo.get('/api/suppliers')
def suppliers():
    return response(suppliers=get_store().list_suppliers(workspace_id()))


@demo.post('/api/suppliers')
def save_supplier():
    return response(supplier=get_store().save_supplier(workspace_id(), body()))


@demo.get('/api/mock/operators')
def operators():
    return response(operators=get_store().operators(workspace_id(), request.args.get('business', 'sms')))


@demo.post('/api/mock/operators')
def save_operator():
    return response(operator=get_store().save_operator(workspace_id(), body()))


@demo.get('/api/mock/rates')
def rates():
    identifier, business = supplier_context(request.args)
    return response(supplier_id=identifier, business=business, timezone='Asia/Shanghai',
                    rates=get_store().query_rates(workspace_id(), identifier, business, None))


@demo.post('/api/mock/query')
def query():
    data = body({'supplier_id', 'business', 'keys', 'direction'})
    identifier, business = supplier_context(data)
    if not isinstance(data.get('keys'), list):
        raise ValueError('keys必须是mcc:mnc文本数组')
    return response(supplier_id=identifier, business=business, timezone='Asia/Shanghai',
                    rates=get_store().query_rates(workspace_id(), identifier, business, data['keys']))


def import_request(maintenance=False):
    data = body({'supplier_id', 'business', 'rows', 'confirm_increases', 'confirmed', 'direction'})
    if maintenance and data.get('confirmed') is not True:
        raise ValueError('维护模拟基准报价必须显式设置confirmed=true')
    identifier, business = supplier_context(data)
    direction = data.get('direction')
    if direction is not None:
        if not isinstance(data.get('rows'), list):
            raise ValueError('rows必须是数组')
        required = {'mt': {'downstream'}, 'mo': {'upstream'}, 'both': {'downstream', 'upstream'}}[direction]
        for row in data['rows']:
            if not isinstance(row, dict) or not required.issubset(row):
                raise ValueError('报价行缺少所选方向的费率')
            if direction != 'both' and ({'downstream', 'upstream'} - required) & set(row):
                raise ValueError('报价行包含所选方向以外的费率')
    return jsonify(get_store().import_rows(workspace_id(), identifier, business, data.get('rows'), data.get('confirm_increases', False)))


@demo.post('/api/mock/import')
def import_rows():
    return import_request()


@demo.post('/api/mock/rates')
def save_rates():
    return import_request(maintenance=True)


@demo.post('/api/mock/reset')
def reset():
    return jsonify(get_store().reset(workspace_id()))


@demo.get('/api/mock/receipts')
def receipts():
    return response(receipts=get_store().receipts(workspace_id()))


@demo.get('/api/examples/email.eml')
def example_email():
    content = (
        'From: simulated-supplier@example.invalid\r\n'
        'To: simulated-customer@example.invalid\r\n'
        'Subject: SIMULATED DATA ONLY - Demo rate update\r\n'
        'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
        'This email and all example rates are simulated. No real Viv connection.\r\n'
        'Effective date:2026-11-15 09:00 UTC\r\n'
    )
    return send_file(BytesIO(content.encode('utf-8')), mimetype='message/rfc822', as_attachment=True, download_name='simulated-email.eml', max_age=0)


@demo.get('/api/examples/<name>')
def example_workbook(name):
    if name not in {'sms_mt', 'sms_both', 'sms_mo', 'voice'}:
        raise ValueError('未知模拟示例名称')
    is_voice = name == 'voice'
    headers = ['CC' if is_voice else 'MCC', 'MNC', 'Outbound' if is_voice else 'New Price', 'Inbound' if is_voice else 'MO', 'Effective Date']
    if is_voice:
        rows = [['44', '10', 0.05, 0.007, '2026-11-15 09:00:00'], ['1', '260', 0.01, 0.003, '']]
    else:
        rows = [
            ['289', '88', 0.04, 0.008, '2026-11-15 09:00:00'],
            ['234', '10', 0.019, 0.004, ''], ['234', '15', 0.03, 0.006, '2026-11-15 09:00:00'],
            ['310', 'N/D', 0.04, 0.009, ''], ['234', '99', 0.06, 0.01, '2026-11-15 09:00:00'],
            ['460', '00', 0.015, 0.003, '2026-11-15 09:00:00'],
            ['502', '12', 0.02, 0.004, '2026-11-15 09:00:00'], ['502', '16', 0.05, 0.009, ''],
        ]
        if name == 'sms_mt':
            rows = [row[:3] + [''] + row[4:] for row in rows]
        elif name == 'sms_mo':
            rows = [row[:2] + [''] + row[3:] for row in rows]
    workbook = xlwt.Workbook(encoding='utf-8')
    sheet = workbook.add_sheet('Pricing_')
    sheet.write(0, 0, MOCK_NOTICE)
    for column, heading in enumerate(headers):
        sheet.write(1, column, heading)
    for row_number, row in enumerate(rows, start=2):
        for column, value in enumerate(row):
            sheet.write(row_number, column, value)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output, mimetype='application/vnd.ms-excel', as_attachment=True, download_name=f'simulated-{name}.xls', max_age=0)
