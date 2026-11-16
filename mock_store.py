import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MOCK_NOTICE = '全部供应商、运营商和费率均为模拟数据，未连接真实 Viv。MNC 999 仅为本演示的通配映射，不代表真实系统约定。'
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ROWS = 20000
MAX_WORKSPACES = 200
MAX_SUPPLIERS = 50
MAX_OPERATORS = 20000
MAX_RATES = 50000
MAX_RECEIPTS = 200
WORKSPACE_TTL_DAYS = 30
CURRENCIES = {'EUR', 'USD', 'GBP', 'CNY', 'HKD', 'SGD', 'CAD', 'AUD', 'JPY', 'CHF'}
SUPPLIER_DEFAULTS = {
    'business': 'sms', 'timezone': 'UTC', 'currency': 'EUR', 'divisor': '1',
    'headers': {'mcc': 'MCC', 'mnc': 'MNC', 'mt': 'New Price', 'mo': 'MO', 'date': 'Effective Date'},
    'missing_date_policy': 'email_then_system', 'invalid_code_policy': 'max',
    'fallback_mnc': '999', 'contract_rate': '5', 'contract_currency': 'USD',
    'contract_fx_rate': '',
}
SEED_SUPPLIERS = [
    SUPPLIER_DEFAULTS | {
        'id': 'skyline-sms', 'name': '模拟 Skyline SMS', 'divisor': '100',
        'headers': {'mcc': 'MCC', 'mnc': 'MNC', 'mt': 'New Price (EUR/100)', 'mo': '', 'date': 'Price Valid from'},
    },
    SUPPLIER_DEFAULTS | {'id': 'standard-sms', 'name': '模拟 Standard SMS'},
    SUPPLIER_DEFAULTS | {
        'id': 'voice-demo', 'name': '模拟语音供应商', 'business': 'voice', 'currency': 'USD',
        'headers': {'mcc': 'CC', 'mnc': 'MNC', 'mt': 'Outbound', 'mo': 'Inbound', 'date': 'Effective Date'},
    },
    SUPPLIER_DEFAULTS | {
        'id': 'tencent-sms', 'name': '腾讯样表适配（演示币种USD，需核对）', 'currency': 'USD',
        'headers': {'mcc': '', 'mnc': '', 'mt': 'Price', 'mo': '', 'date': '', 'country': 'MCCMNC'},
        'country_map': {'730': {'mcc': '730', 'mnc': '999'}, '5051': {'mcc': '505', 'mnc': '999'},
                        '286': {'mcc': '286', 'mnc': '999'}, '724': {'mcc': '724', 'mnc': '999'}},
    },
    SUPPLIER_DEFAULTS | {
        'id': 'volcano-sms', 'name': '火山样表适配（演示数据）', 'currency': 'USD', 'sheet_name': '混合',
        'headers': {'mcc': 'MCC', 'mnc': '', 'mt': '报价（USD）', 'mo': '', 'date': ''},
    },
]
SMS_CODES = [('289', '88'), ('234', '10'), ('234', '15'), ('310', '260'), ('460', '00'), ('502', '12'), ('502', '16'),
             ('730', '999'), ('505', '999'), ('286', '999'), ('724', '999')]
VOICE_CODES = [('44', '10'), ('44', '15'), ('1', '260')]
SEED_TIME = '2026-11-01 00:00:00'


def json_size(value, limit=MAX_JSON_BYTES):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError('需要合法、有限数值的 JSON 数据') from error
    if len(encoded.encode('utf-8')) > limit:
        raise ValueError('JSON 数据超过大小限制')
    return encoded


def text(value, label, maximum=120, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not value.strip() and not empty):
        raise ValueError(f'{label}必须是长度不超过{maximum}的文本')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f'{label}不能包含控制字符')
    return value.strip()


def decimal_text(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{label}必须是十进制数值')
    raw = str(value)
    if len(raw) > 64:
        raise ValueError(f'{label}过长')
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or amount > Decimal('1000000000'):
            raise ValueError(f'{label}必须是0至1000000000之间的有限数值')
        if amount.as_tuple().exponent < -12 or (positive and amount == 0):
            raise ValueError(f'{label}最多12位小数，汇率必须大于0')
        rendered = format(amount, 'f')
        return (rendered.rstrip('0').rstrip('.') if '.' in rendered else rendered) if amount else '0'
    except InvalidOperation as error:
        raise ValueError(f'{label}必须是十进制数值') from error


def validate_supplier(data):
    if not isinstance(data, dict):
        raise ValueError('供应商配置必须是对象')
    json_size(data, 128 * 1024)
    if set(data) - (set(SUPPLIER_DEFAULTS) | {'id', 'name', 'country_map', 'sheet_name'}):
        raise ValueError('供应商配置包含不支持的字段')
    supplier = SUPPLIER_DEFAULTS | data
    identifier = text(supplier.get('id'), '供应商ID', 64)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', identifier):
        raise ValueError('供应商ID仅允许字母、数字、下划线和连字符')
    supplier['id'] = identifier
    supplier['name'] = text(supplier.get('name'), '供应商名称', 120)
    choices = {
        'business': {'sms', 'voice'}, 'currency': CURRENCIES, 'contract_currency': CURRENCIES,
        'divisor': {'100', '1'}, 'missing_date_policy': {'email_then_system', 'manual', 'same_file', 'skip'},
        'invalid_code_policy': {'max', 'contract', 'skip'}, 'fallback_mnc': {'999'},
    }
    for field, options in choices.items():
        if not isinstance(supplier[field], str) or supplier[field] not in options:
            raise ValueError(f'供应商{field}配置不合法')
    supplier['timezone'] = text(supplier['timezone'], '时区', 80)
    try:
        ZoneInfo(supplier['timezone'])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('供应商时区无效') from error
    headers = supplier['headers']
    if not isinstance(headers, dict) or set(headers) - {'mcc', 'mnc', 'mt', 'mo', 'date', 'mccmnc', 'country'}:
        raise ValueError('headers只支持mcc、mnc、mt、mo、date、mccmnc、country')
    headers = {'mcc': '', 'mnc': '', 'mt': '', 'mo': '', 'date': ''} | headers
    supplier['headers'] = {
        field: text(value, f'表头{field}', 120, empty=True)
        for field, value in headers.items()
    }
    if not (supplier['headers']['mcc'] or supplier['headers'].get('mccmnc') or supplier['headers'].get('country')):
        raise ValueError('必须配置mcc（MNC可空）、合并mccmnc或country中的至少一种')
    mapped = [value.casefold() for value in supplier['headers'].values() if value]
    if len(mapped) != len(set(mapped)) or not (supplier['headers']['mt'] or supplier['headers']['mo']):
        raise ValueError('关键列映射不能重复，且至少配置一个报价方向')
    supplier['contract_rate'] = decimal_text(supplier['contract_rate'], '合同费率')
    if supplier['contract_fx_rate'] != '':
        supplier['contract_fx_rate'] = decimal_text(supplier['contract_fx_rate'], '合同汇率', positive=True)
    if 'country_map' in supplier:
        mapping = supplier['country_map']
        if not isinstance(mapping, dict) or len(mapping) > 500:
            raise ValueError('country_map必须是最多500项的显式模拟映射')
        checked = {}
        for country, pair in mapping.items():
            identifier = text(country, '国家标识', 120)
            if identifier != country:
                raise ValueError('国家标识不能包含首尾空格')
            if not isinstance(pair, dict) or set(pair) != {'mcc', 'mnc'}:
                raise ValueError('国家映射每项必须仅包含mcc和mnc')
            code_pair(pair['mcc'], pair['mnc'], supplier['business'])
            checked[country] = dict(pair)
        supplier['country_map'] = checked
    if 'sheet_name' in supplier:
        supplier['sheet_name'] = text(supplier['sheet_name'], '工作表名称', 31, empty=True)
        if any(character in '[]:*?/' or character == chr(92) for character in supplier['sheet_name']):
            raise ValueError('工作表名称包含不合法字符')
    return supplier


def code_pair(mcc, mnc, business):
    if not isinstance(business, str) or business not in {'sms', 'voice'}:
        raise ValueError('业务类型必须是sms或voice')
    mcc_pattern = r'[1-9][0-9]{2}' if business == 'sms' else r'[1-9][0-9]{0,2}'
    if not isinstance(mcc, str) or not re.fullmatch(mcc_pattern, mcc):
        raise ValueError('MCC必须为100至999的三位文本；语音CC必须为1至3位非零开头数字')
    if not isinstance(mnc, str) or not re.fullmatch(r'[0-9]{2,3}', mnc):
        raise ValueError('MNC必须为保留前导零的两位或三位数字文本')
    return mcc, mnc


def effective_time_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', value):
        raise ValueError('生效时间必须为YYYY-MM-DD HH:MM:SS')
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OverflowError) as error:
        raise ValueError('生效时间无效；导入时间必须已转换为GMT+8') from error


class MockStore:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get('VIV_DATA_DIR') or Path(__file__).resolve().parent / '.runtime')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'mock.sqlite3'
        with self.connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, last_seen TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS workspace_age ON workspaces(last_seen);
                CREATE TABLE IF NOT EXISTS suppliers (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, config TEXT NOT NULL,
                    PRIMARY KEY (workspace, id)
                );
                CREATE TABLE IF NOT EXISTS operators (
                    workspace TEXT NOT NULL, business TEXT NOT NULL, mcc TEXT NOT NULL,
                    mnc TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (workspace, business, mcc, mnc)
                );
                CREATE TABLE IF NOT EXISTS rates (
                    workspace TEXT NOT NULL, supplier_id TEXT NOT NULL, business TEXT NOT NULL,
                    mcc TEXT NOT NULL, mnc TEXT NOT NULL, downstream TEXT NOT NULL,
                    upstream TEXT NOT NULL, effective_time TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'UNKNOWN',
                    PRIMARY KEY (workspace, supplier_id, business, mcc, mnc)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    workspace TEXT NOT NULL, id TEXT NOT NULL, summary TEXT NOT NULL,
                    PRIMARY KEY (workspace, id)
                );
            ''')
            if 'currency' not in {row['name'] for row in connection.execute('PRAGMA table_info(rates)')}:
                try:
                    connection.execute("ALTER TABLE rates ADD COLUMN currency TEXT NOT NULL DEFAULT 'UNKNOWN'")
                except sqlite3.OperationalError:
                    if 'currency' not in {row['name'] for row in connection.execute('PRAGMA table_info(rates)')}:
                        raise

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=30000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _workspace(self, connection, workspace):
        if not isinstance(workspace, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', workspace):
            raise ValueError('工作区ID无效')
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=WORKSPACE_TTL_DAYS)).isoformat()
        for table in ('suppliers', 'operators', 'rates', 'receipts'):
            connection.execute(f'DELETE FROM {table} WHERE workspace IN (SELECT id FROM workspaces WHERE last_seen < ?)', (cutoff,))
        connection.execute('DELETE FROM workspaces WHERE last_seen < ?', (cutoff,))
        existing = connection.execute('SELECT id FROM workspaces WHERE id=?', (workspace,)).fetchone()
        if existing:
            connection.execute('UPDATE workspaces SET last_seen=? WHERE id=?', (now.isoformat(), workspace))
            return
        if connection.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0] >= MAX_WORKSPACES:
            raise ValueError('模拟工作区数量已达上限，请等待过期工作区清理')
        connection.execute('INSERT INTO workspaces VALUES (?, ?)', (workspace, now.isoformat()))
        for business, codes in (('sms', SMS_CODES), ('voice', VOICE_CODES)):
            pairs = list(dict.fromkeys(codes + [(mcc, '999') for mcc in sorted({pair[0] for pair in codes})]))
            for mcc, mnc in pairs:
                name = f'模拟运营商 {mcc}:{mnc}' if mnc != '999' else f'模拟通配映射 {mcc}:999（非真实系统约定）'
                connection.execute('INSERT INTO operators VALUES (?, ?, ?, ?, ?)', (workspace, business, mcc, mnc, name))
        for supplier in SEED_SUPPLIERS:
            connection.execute('INSERT INTO suppliers VALUES (?, ?, ?)', (workspace, supplier['id'], json_size(supplier)))
            codes = SMS_CODES if supplier['business'] == 'sms' else VOICE_CODES
            pairs = list(dict.fromkeys(codes + [(mcc, '999') for mcc in sorted({pair[0] for pair in codes})]))
            for index, (mcc, mnc) in enumerate(pairs):
                connection.execute('INSERT INTO rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
                    workspace, supplier['id'], supplier['business'], mcc, mnc,
                    str(Decimal('0.02') + Decimal(index) / 1000), '0.005', SEED_TIME, supplier['currency'],
                ))

    def _supplier(self, connection, workspace, supplier_id, business=None):
        if not isinstance(supplier_id, str) or len(supplier_id) > 64:
            raise ValueError('供应商ID必须是长度不超过64的文本')
        row = connection.execute('SELECT config FROM suppliers WHERE workspace=? AND id=?', (workspace, supplier_id)).fetchone()
        if row is None:
            raise ValueError('当前工作区不存在该供应商')
        supplier = json.loads(row['config'])
        if business is not None and (not isinstance(business, str) or business not in {'sms', 'voice'} or supplier['business'] != business):
            raise ValueError('业务类型与当前供应商不匹配')
        return supplier

    def get_supplier(self, workspace, supplier_id):
        with self.connection() as connection:
            self._workspace(connection, workspace)
            return self._supplier(connection, workspace, supplier_id)

    def list_suppliers(self, workspace):
        with self.connection() as connection:
            self._workspace(connection, workspace)
            return [json.loads(row['config']) for row in connection.execute('SELECT config FROM suppliers WHERE workspace=? ORDER BY id', (workspace,))]

    def save_supplier(self, workspace, data):
        supplier = validate_supplier(data)
        with self.connection() as connection:
            self._workspace(connection, workspace)
            existing = connection.execute('SELECT config FROM suppliers WHERE workspace=? AND id=?', (workspace, supplier['id'])).fetchone()
            if existing and any(json.loads(existing['config'])[field] != supplier[field] for field in ('business', 'currency')):
                if connection.execute('SELECT 1 FROM rates WHERE workspace=? AND supplier_id=? LIMIT 1', (workspace, supplier['id'])).fetchone():
                    raise ValueError('已有基准报价的供应商不能更换业务类型或币种，请新建供应商或重置工作区')
            if not existing and connection.execute('SELECT COUNT(*) FROM suppliers WHERE workspace=?', (workspace,)).fetchone()[0] >= MAX_SUPPLIERS:
                raise ValueError('工作区供应商配置已达上限')
            connection.execute('INSERT OR REPLACE INTO suppliers VALUES (?, ?, ?)', (workspace, supplier['id'], json_size(supplier)))
        return supplier

    def operators(self, workspace, business='sms'):
        if not isinstance(business, str) or business not in {'sms', 'voice'}:
            raise ValueError('业务类型必须是sms或voice')
        with self.connection() as connection:
            self._workspace(connection, workspace)
            return [dict(row) for row in connection.execute('SELECT business, mcc, mnc, name FROM operators WHERE workspace=? AND business=? ORDER BY mcc, mnc', (workspace, business))]

    def save_operator(self, workspace, data):
        if not isinstance(data, dict) or set(data) != {'business', 'mcc', 'mnc', 'name'}:
            raise ValueError('运营商必须包含business、mcc、mnc、name四个字段')
        json_size(data, 2048)
        pair = code_pair(data['mcc'], data['mnc'], data['business'])
        operator = data | {'name': text(data['name'], '模拟运营商名称', 120)}
        with self.connection() as connection:
            self._workspace(connection, workspace)
            existing = connection.execute('SELECT 1 FROM operators WHERE workspace=? AND business=? AND mcc=? AND mnc=?', (workspace, data['business'], *pair)).fetchone()
            if not existing and connection.execute('SELECT COUNT(*) FROM operators WHERE workspace=?', (workspace,)).fetchone()[0] >= MAX_OPERATORS:
                raise ValueError('工作区模拟运营商数量已达上限')
            connection.execute('INSERT OR REPLACE INTO operators VALUES (?, ?, ?, ?, ?)', (workspace, operator['business'], *pair, operator['name']))
        return operator

    def _rates(self, connection, workspace, supplier_id, business):
        return {(row['mcc'], row['mnc']): {
            'downstream': row['downstream'], 'upstream': row['upstream'],
            'effective_time': row['effective_time'], 'currency': row['currency'], 'timezone': 'Asia/Shanghai',
        } for row in connection.execute('SELECT * FROM rates WHERE workspace=? AND supplier_id=? AND business=? ORDER BY mcc, mnc', (workspace, supplier_id, business))}

    def query_rates(self, workspace, supplier_id, business, keys=None):
        if not isinstance(business, str) or business not in {'sms', 'voice'}:
            raise ValueError('业务类型必须是sms或voice')
        if keys is not None:
            if not isinstance(keys, list) or len(keys) > MAX_ROWS:
                raise ValueError('keys必须是最多20000项的数组')
            for key in keys:
                if not isinstance(key, str) or key.count(':') != 1:
                    raise ValueError('查询键必须为mcc:mnc文本')
                code_pair(*key.split(':'), business)
        with self.connection() as connection:
            self._workspace(connection, workspace)
            self._supplier(connection, workspace, supplier_id, business)
            selected = None if keys is None else set(keys)
            return {f'{pair[0]}:{pair[1]}': rate for pair, rate in self._rates(connection, workspace, supplier_id, business).items()
                    if selected is None or f'{pair[0]}:{pair[1]}' in selected}

    def import_rows(self, workspace, supplier_id, business, rows, confirm_increases=False):
        if type(confirm_increases) is not bool:
            raise ValueError('confirm_increases必须是布尔值')
        if not isinstance(business, str) or business not in {'sms', 'voice'}:
            raise ValueError('业务类型必须是sms或voice')
        if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
            raise ValueError('rows必须是包含1至20000行的数组')
        json_size(rows)
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._workspace(connection, workspace)
            supplier = self._supplier(connection, workspace, supplier_id, business)
            if connection.execute('SELECT COUNT(*) FROM receipts WHERE workspace=?', (workspace,)).fetchone()[0] >= MAX_RECEIPTS:
                raise ValueError('工作区导入回执数量已达上限，请重置工作区后继续')
            known = {(row['mcc'], row['mnc']) for row in connection.execute('SELECT mcc, mnc FROM operators WHERE workspace=? AND business=?', (workspace, business))}
            current = self._rates(connection, workspace, supplier_id, business)
            normalized = {}
            increases = []
            for row in rows:
                if not isinstance(row, dict) or set(row) - {'mcc', 'mnc', 'downstream', 'upstream', 'effective_time', 'currency', 'supplier_id', 'business'}:
                    raise ValueError('报价行必须是仅包含规范字段的对象')
                if row.get('supplier_id', supplier_id) != supplier_id or row.get('business', business) != business:
                    raise ValueError('报价行的供应商或业务类型与当前请求不匹配')
                if row.get('currency') != supplier['currency']:
                    raise ValueError('导入报价币种必须与当前供应商配置一致，不自动换汇')
                pair = code_pair(row.get('mcc'), row.get('mnc'), business)
                if pair not in known:
                    raise ValueError(f'编码组合不在模拟运营商映射中：{pair[0]}:{pair[1]}')
                effective_time = effective_time_value(row.get('effective_time'))
                supplied = {field: decimal_text(row[field], field) for field in ('downstream', 'upstream') if field in row}
                if not supplied:
                    raise ValueError('报价行至少需要一个方向的费率')
                previous = current.get(pair)
                if previous and previous['currency'] != supplier['currency']:
                    raise ValueError('历史基准报价币种与当前供应商不一致，请新建供应商或重置工作区')
                if previous is None and len(supplied) != 2:
                    raise ValueError('新增编码需提供上下行两个方向的初始费率')
                prices = {field: supplied.get(field, previous[field] if previous else '0') for field in ('downstream', 'upstream')}
                candidate = prices | {'effective_time': effective_time}
                if pair in normalized:
                    if normalized[pair] != candidate:
                        raise ValueError(f'同一编码重复且报价或生效时间不同：{pair[0]}:{pair[1]}')
                    continue
                normalized[pair] = candidate
                if previous and any(Decimal(prices[field]) > Decimal(previous[field]) for field in prices):
                    increases.append(f'{pair[0]}:{pair[1]}')
            if increases and not confirm_increases:
                raise ValueError('检测到涨价，必须复核并设置confirm_increases=true：' + ', '.join(increases[:10]))
            total = connection.execute('SELECT COUNT(*) FROM rates WHERE workspace=?', (workspace,)).fetchone()[0]
            if total + len(set(normalized) - set(current)) > MAX_RATES:
                raise ValueError('工作区基准报价数量已达上限')
            connection.executemany('INSERT OR REPLACE INTO rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                (workspace, supplier_id, business, *pair, candidate['downstream'], candidate['upstream'], candidate['effective_time'], supplier['currency'])
                for pair, candidate in normalized.items()
            ])
            receipt = {
                'receipt_id': str(uuid4()), 'imported': len(normalized), 'submitted': len(rows),
                'deduplicated': len(rows) - len(normalized), 'supplier_id': supplier_id, 'business': business,
                'currency': supplier['currency'], 'timezone': 'Asia/Shanghai', 'mode': 'mock', 'notice': MOCK_NOTICE,
                'increases': len(increases), 'confirm_increases': confirm_increases,
                'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                'created_at_timezone': 'UTC',
            }
            connection.execute('INSERT INTO receipts VALUES (?, ?, ?)', (workspace, receipt['receipt_id'], json_size(receipt)))
        return receipt

    def receipts(self, workspace):
        with self.connection() as connection:
            self._workspace(connection, workspace)
            return [json.loads(row['summary']) for row in connection.execute('SELECT summary FROM receipts WHERE workspace=? ORDER BY rowid DESC', (workspace,))]

    def reset(self, workspace):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._workspace(connection, workspace)
            for table in ('suppliers', 'operators', 'rates', 'receipts', 'workspaces'):
                column = 'id' if table == 'workspaces' else 'workspace'
                connection.execute(f'DELETE FROM {table} WHERE {column}=?', (workspace,))
            self._workspace(connection, workspace)
        return {'mode': 'mock', 'reset': True, 'notice': MOCK_NOTICE}
