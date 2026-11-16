from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_store
from demo_api import get_store, persistent_secret, register_demo, workspace_id
from mock_store import MockStore, SEED_SUPPLIERS, validate_supplier


def quote(**updates):
    return {'mcc': '289', 'mnc': '88', 'downstream': '0.01', 'upstream': '0.004',
            'effective_time': '2026-11-15 09:00:00', 'currency': 'EUR'} | updates


class MockSystemTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / '.agent' / 'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='mock-test-', dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ('VIV_SECRET_KEY', 'SECRET_KEY', 'VIV_DATA_DIR', 'VIV_SECURE_COOKIE'):
            os.environ.pop(name, None)
        self.app = self.make_app()
        self.store = self.app.extensions['viv_mock_store']
        self.client = self.app.test_client()

    def make_app(self, **config):
        application = Flask(__name__)
        application.config.update(TESTING=True, VIV_DATA_DIR=self.temporary.name, **config)
        register_demo(application)
        return application

    def rates(self, workspace='alpha', supplier='standard-sms', business='sms', keys=None):
        return self.store.query_rates(workspace, supplier, business, keys)

    def import_rows(self, rows, workspace='alpha', confirmed=False, supplier='standard-sms', business='sms'):
        return self.store.import_rows(workspace, supplier, business, rows, confirmed)

    def test_seed_contract_and_explicit_presets(self):
        suppliers = self.store.list_suppliers('alpha')
        self.assertEqual({item['id'] for item in suppliers}, {item['id'] for item in SEED_SUPPLIERS})
        for supplier in suppliers:
            self.assertEqual(validate_supplier(supplier), supplier)
        expected = {'289:88', '234:10', '234:15', '310:260', '460:00', '502:12', '502:16', '234:999'}
        self.assertTrue(expected.issubset(self.rates()))
        self.assertEqual(self.rates()['289:88']['effective_time'], '2026-11-01 00:00:00')
        self.assertEqual(self.rates()['289:88']['currency'], 'EUR')
        self.assertIn('44:10', self.rates(supplier='voice-demo', business='voice'))
        tencent = self.store.get_supplier('alpha', 'tencent-sms')
        self.assertEqual(tencent['country_map']['5051'], {'mcc': '505', 'mnc': '999'})
        self.assertEqual(tencent['headers']['country'], 'MCCMNC')
        self.assertEqual(self.store.get_supplier('alpha', 'volcano-sms')['headers']['mnc'], '')

    def test_browser_isolation_and_workspace_reset(self):
        other = self.app.test_client()
        baseline = self.client.get('/api/mock/rates?supplier_id=standard-sms').json['rates']
        other.get('/api/suppliers')
        with self.client.session_transaction() as state:
            first_workspace = state['viv_workspace_id']
        with other.session_transaction() as state:
            second_workspace = state['viv_workspace_id']
        self.assertNotEqual(first_workspace, second_workspace)
        self.client.post('/api/mock/import', json={'supplier_id': 'standard-sms', 'rows': [quote()]})
        self.client.post('/api/suppliers', json={'id': 'private-sms', 'name': '模拟私有配置'})
        self.client.post('/api/mock/operators', json={'business': 'sms', 'mcc': '123', 'mnc': '01', 'name': '模拟私有运营商'})
        self.assertEqual(other.get('/api/mock/rates?supplier_id=standard-sms').json['rates'], baseline)
        self.assertNotIn('private-sms', {item['id'] for item in other.get('/api/suppliers').json['suppliers']})
        self.assertFalse(other.get('/api/mock/receipts').json['receipts'])
        self.assertEqual(self.client.post('/api/mock/reset').status_code, 200)
        self.assertEqual(self.client.get('/api/mock/rates?supplier_id=standard-sms').json['rates'], baseline)
        self.assertFalse(self.client.get('/api/mock/receipts').json['receipts'])
        self.assertNotIn('123', {item['mcc'] for item in self.client.get('/api/mock/operators').json['operators']})

    def test_persistent_database_secret_and_signed_session(self):
        self.client.get('/api/suppliers')
        result = self.client.post('/api/mock/import', json={'supplier_id': 'standard-sms', 'rows': [quote()]})
        self.assertEqual(result.status_code, 200)
        restarted = self.make_app()
        self.assertEqual(restarted.secret_key, self.app.secret_key)
        resumed = restarted.test_client()
        resumed.set_cookie('session', self.client.get_cookie('session').value)
        self.assertEqual(resumed.get('/api/mock/rates?supplier_id=standard-sms').json['rates']['289:88']['downstream'], '0.01')
        self.assertEqual(resumed.get('/api/mock/receipts').json['receipts'][0]['receipt_id'], result.json['receipt_id'])
        resumed.set_cookie('session', self.client.get_cookie('session').value + 'tampered')
        self.assertEqual(resumed.get('/api/mock/rates?supplier_id=standard-sms').json['rates']['289:88']['downstream'], '0.02')
        with self.app.test_request_context('/'):
            self.assertIs(get_store(), self.store)
            self.assertEqual(workspace_id(), workspace_id())

    def test_secure_cookie_follows_http_https_and_proxy(self):
        application = self.make_app(VIV_SECURE_COOKIE=True)
        for base_url, headers, secure in [
            ('http://localhost', {}, False), ('https://localhost', {}, True),
            ('http://localhost', {'X-Forwarded-Proto': 'https'}, True),
        ]:
            with self.subTest(base_url=base_url, headers=headers):
                client = application.test_client()
                result = client.get('/api/suppliers', base_url=base_url, headers=headers)
                cookie = result.headers['Set-Cookie']
                self.assertEqual('; Secure;' in cookie, secure)
                self.assertIn('HttpOnly', cookie)
                self.assertIn('SameSite=Lax', cookie)
                self.assertNotIn('Set-Cookie', client.get('/api/suppliers', base_url=base_url, headers=headers).headers)

    def test_strict_import_validation_and_atomic_rollback(self):
        baseline = self.rates()
        invalid = [
            quote(mcc='234', mnc='99'), quote(mcc=289), quote(mcc='000'), quote(mnc='0'),
            quote(mnc='００'), quote(downstream='NaN'), quote(downstream='Infinity'),
            quote(downstream='-0.1'), quote(downstream=True), quote(downstream='1e-1000000'),
            quote(downstream='1000000001'), quote(downstream='0.0000000000001'),
            quote(effective_time='2026-02-30 00:00:00'), quote(effective_time=''),
            quote(effective_time='2026-11-15'), quote(currency='USD'), quote(currency=None),
            quote(supplier_id='voice-demo'), quote(business='voice'), quote(url='https://invalid.example'),
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.import_rows([quote(mcc='234', mnc='10'), row], confirmed=True)
            self.assertEqual(self.rates(), baseline)
        for rows in ([], {}, None):
            with self.assertRaises(ValueError):
                self.import_rows(rows)
        self.assertEqual(self.store.receipts('alpha'), [])
        with self.assertRaises(ValueError):
            self.import_rows([quote()], business='voice')

    def test_increases_rechecked_against_current_database(self):
        with self.assertRaises(ValueError):
            self.import_rows([quote(downstream='0.04')])
        for confirmation in ('true', 1, None):
            with self.assertRaises(ValueError):
                self.import_rows([quote()], confirmed=confirmation)
        receipt = self.import_rows([quote(downstream='0.04')], confirmed=True)
        self.assertEqual(receipt['increases'], 1)
        self.assertEqual(receipt['mode'], 'mock')
        self.assertNotIn('rows', self.store.receipts('alpha')[0])
        self.import_rows([quote(downstream='0.005')])
        with self.assertRaises(ValueError):
            self.import_rows([quote(downstream='0.01')])

    def test_duplicates_cannot_silently_override(self):
        for changed in (quote(downstream='0.009'), quote(effective_time='2026-11-16 09:00:00')):
            with self.assertRaises(ValueError):
                self.import_rows([quote(), changed], confirmed=True)
        receipt = self.import_rows([quote(), quote(downstream='0.0100')])
        self.assertEqual((receipt['imported'], receipt['deduplicated']), (1, 1))

    def test_single_direction_preserves_other_and_voice_codes(self):
        row = quote()
        del row['upstream']
        self.import_rows([row])
        self.assertEqual(self.rates()['289:88']['upstream'], '0.005')
        self.import_rows([quote(mcc='44', mnc='10', currency='USD')], supplier='voice-demo', business='voice')
        self.assertEqual(self.rates(supplier='voice-demo', business='voice')['44:10']['downstream'], '0.01')
        self.store.save_operator('alpha', {'business': 'sms', 'mcc': '123', 'mnc': '01', 'name': '模拟新增'})
        with self.assertRaises(ValueError):
            self.import_rows([row | {'mcc': '123', 'mnc': '01'}])

    def test_supplier_configuration_validation(self):
        base = self.store.get_supplier('alpha', 'standard-sms')
        invalid = [
            {'timezone': 'Not/AZone'}, {'timezone': '../UTC'}, {'name': 'a' * 121},
            {'divisor': '0'}, {'currency': []}, {'business': 'fax'}, {'contract_rate': '-1'},
            {'contract_fx_rate': '0'}, {'contract_rate': '1e999999'}, {'file_path': 'C:/secret'},
            {'headers': {'mcc': 'Code', 'mnc': 'code', 'mt': 'Price'}},
            {'headers': {'mnc': 'MNC', 'mt': 'Price'}}, {'headers': {'mcc': 'MCC'}},
            {'country_map': {'country': {'mcc': '123', 'mnc': '1'}}},
            {'country_map': {str(index): {'mcc': '123', 'mnc': '01'} for index in range(501)}},
            {'country_map': {' bad ': {'mcc': '123', 'mnc': '01'}}}, {'sheet_name': '../file'},
        ]
        for update in invalid:
            with self.subTest(fields=list(update)), self.assertRaises(ValueError):
                self.store.save_supplier('alpha', base | update)
        for headers in ({'mcc': 'MCC', 'mt': 'Price'}, {'mccmnc': 'MCCMNC', 'mt': 'Price'}, {'country': 'Country', 'mt': 'Price'}):
            saved = self.store.save_supplier('alpha', base | {'headers': headers})
            self.assertEqual(saved['headers']['mnc'], '')
        mapping = {str(index): {'mcc': '123', 'mnc': '01'} for index in range(500)}
        self.store.save_supplier('alpha', base | {'country_map': mapping})
        with self.assertRaises(ValueError):
            self.store.save_supplier('alpha', base | {'business': 'voice'})

    def test_operator_maintenance_and_rates_require_confirmation(self):
        operator = {'business': 'sms', 'mcc': '123', 'mnc': '01', 'name': '模拟新增'}
        self.assertEqual(self.client.post('/api/mock/operators', json=operator).status_code, 200)
        self.assertEqual(self.client.post('/api/mock/operators', json=operator | {'name': '模拟更新'}).status_code, 200)
        for update in ({'mcc': '00'}, {'mcc': '000'}, {'mnc': '1'}, {'name': 'x' * 121}, {'business': 'email'}):
            self.assertEqual(self.client.post('/api/mock/operators', json=operator | update).status_code, 400)
        payload = {'supplier_id': 'standard-sms', 'rows': [quote(downstream='0.04')]}
        self.assertEqual(self.client.post('/api/mock/rates', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/mock/rates', json=payload | {'confirmed': True}).status_code, 400)
        self.assertEqual(self.client.post('/api/mock/rates', json=payload | {'confirmed': True, 'confirm_increases': True}).status_code, 200)
        self.assertEqual(self.client.post('/api/mock/rates', json=payload | {'rows': [quote(mnc='99')], 'confirmed': True, 'confirm_increases': True}).status_code, 400)

    def test_import_dates_are_already_normalized_to_gmt8(self):
        supplier = self.store.get_supplier('alpha', 'standard-sms')
        self.store.save_supplier('alpha', supplier | {'timezone': 'Asia/Shanghai'})
        self.import_rows([quote(effective_time='2026-11-15 08:00:00')])
        self.assertEqual(self.rates()['289:88']['effective_time'], '2026-11-15 08:00:00')
        self.assertEqual(self.rates()['289:88']['timezone'], 'Asia/Shanghai')
        self.store.save_supplier('alpha', supplier | {'timezone': 'America/New_York'})
        for date in ('2026-02-30 02:30:00', '2026-11-01 25:30:00'):
            with self.assertRaises(ValueError):
                self.import_rows([quote(effective_time=date)])

    def test_examples_are_single_sheet_biff8_with_notice(self):
        for name in ('sms_mt', 'sms_both', 'sms_mo', 'voice'):
            result = self.client.get('/api/examples/' + name)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data[:8], bytes.fromhex('d0cf11e0a1b11ae1'))
            workbook = xlrd.open_workbook(file_contents=result.data)
            self.assertEqual(workbook.sheet_names(), ['Pricing_'])
            sheet = workbook.sheet_by_index(0)
            self.assertIn('模拟', sheet.cell_value(0, 0))
            self.assertEqual(sheet.cell_value(1, 0), 'CC' if name == 'voice' else 'MCC')
            if name == 'sms_both':
                self.assertIn(['460', '00'], [sheet.row_values(index)[:2] for index in range(2, sheet.nrows)])
                self.assertIn('N/D', sheet.col_values(1))
        email = BytesParser(policy=policy.default).parsebytes(self.client.get('/api/examples/email.eml').data)
        self.assertIn('Effective date:2026-11-15 09:00 UTC', email.get_content())
        self.assertIn('SIMULATED', email['Subject'])

    def test_json_boundary_and_direction_validation(self):
        for raw in ('[]', '{broken', '{"rows":NaN}'):
            self.assertEqual(self.client.post('/api/mock/import', data=raw, content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post('/api/mock/import', data={'file': 'anything.xls'}).status_code, 400)
        with patch('demo_api.MAX_JSON_BYTES', 10):
            self.assertEqual(self.client.post('/api/mock/import', json={'supplier_id': 'standard-sms', 'rows': [quote()]}).status_code, 400)
        payload = {'supplier_id': 'standard-sms', 'rows': [quote()], 'direction': 'mo'}
        self.assertEqual(self.client.post('/api/mock/import', json=payload).status_code, 400)
        row = quote()
        del row['downstream']
        self.assertEqual(self.client.post('/api/mock/import', json=payload | {'rows': [row]}).status_code, 200)
        self.assertEqual(self.client.post('/api/mock/query', json={'supplier_id': 'standard-sms', 'keys': '234:10'}).status_code, 400)

    def test_currency_is_persisted_and_old_rates_cannot_be_relabelled(self):
        self.assertEqual(self.rates(supplier='voice-demo', business='voice')['44:10']['currency'], 'USD')
        supplier = self.store.get_supplier('alpha', 'standard-sms')
        with self.assertRaises(ValueError):
            self.store.save_supplier('alpha', supplier | {'currency': 'USD'})
        self.assertEqual(self.rates()['289:88']['currency'], 'EUR')
        with self.assertRaises(ValueError):
            self.import_rows([quote(mcc='44', mnc='10')], supplier='voice-demo', business='voice')
        with self.store.connection() as connection:
            connection.execute("UPDATE rates SET currency='GBP' WHERE workspace='alpha' AND supplier_id='standard-sms' AND mcc='289' AND mnc='88'")
        with self.assertRaises(ValueError):
            self.import_rows([quote()], confirmed=True)
        self.assertEqual(self.rates()['289:88']['currency'], 'GBP')
        self.store.reset('alpha')
        self.assertEqual(self.rates(supplier='voice-demo', business='voice')['44:10']['currency'], 'USD')

    def test_legacy_currency_migration_does_not_guess(self):
        self.store.list_suppliers('alpha')
        with self.store.connection() as connection:
            connection.executescript('''
                ALTER TABLE rates RENAME TO rates_new;
                CREATE TABLE rates (
                    workspace TEXT NOT NULL, supplier_id TEXT NOT NULL, business TEXT NOT NULL,
                    mcc TEXT NOT NULL, mnc TEXT NOT NULL, downstream TEXT NOT NULL,
                    upstream TEXT NOT NULL, effective_time TEXT NOT NULL,
                    PRIMARY KEY (workspace, supplier_id, business, mcc, mnc)
                );
                INSERT INTO rates SELECT workspace, supplier_id, business, mcc, mnc, downstream, upstream, effective_time FROM rates_new;
                DROP TABLE rates_new;
            ''')
        migrated = MockStore(self.temporary.name)
        self.assertEqual(migrated.query_rates('alpha', 'voice-demo', 'voice', ['44:10'])['44:10']['currency'], 'UNKNOWN')
        with self.assertRaises(ValueError):
            migrated.import_rows('alpha', 'voice-demo', 'voice', [quote(mcc='44', mnc='10', currency='USD')], True)
        self.assertEqual(migrated.query_rates('new', 'voice-demo', 'voice', ['44:10'])['44:10']['currency'], 'USD')

    def test_2591_keys_use_one_rates_query_and_batch_import(self):
        self.store.list_suppliers('alpha')
        pairs = [(str(200 + index // 1000), f'{index % 1000:03d}') for index in range(2591)]
        with self.store.connection() as connection:
            connection.executemany('INSERT INTO operators VALUES (?, ?, ?, ?, ?)', [
                ('alpha', 'sms', mcc, mnc, '模拟批量运营商') for mcc, mnc in pairs])
            connection.executemany('INSERT INTO rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                ('alpha', 'standard-sms', 'sms', mcc, mnc, '0.02', '0.005', '2026-11-01 00:00:00', 'EUR') for mcc, mnc in pairs])
        statements = []
        original = self.store.connection

        @contextmanager
        def traced_connection():
            with original() as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(self.store, 'connection', traced_connection):
            result = self.rates(keys=[f'{mcc}:{mnc}' for mcc, mnc in pairs])
        self.assertEqual(len(result), 2591)
        self.assertEqual(len([statement for statement in statements if statement.startswith('SELECT * FROM rates')]), 1)
        receipt = self.import_rows([quote(mcc=mcc, mnc=mnc) for mcc, mnc in pairs])
        self.assertEqual(receipt['imported'], 2591)
        with self.assertRaises(ValueError):
            self.rates(keys=['234:10'] * 20001)

    def test_capacity_limits_allow_updates_but_block_growth(self):
        self.store.list_suppliers('alpha')
        with patch.object(mock_store, 'MAX_SUPPLIERS', len(SEED_SUPPLIERS)):
            self.store.save_supplier('alpha', self.store.get_supplier('alpha', 'standard-sms'))
            with self.assertRaises(ValueError):
                self.store.save_supplier('alpha', {'id': 'over-limit', 'name': '模拟超额'})
        with self.store.connection() as connection:
            operator_count = connection.execute("SELECT COUNT(*) FROM operators WHERE workspace='alpha'").fetchone()[0]
            rate_count = connection.execute("SELECT COUNT(*) FROM rates WHERE workspace='alpha'").fetchone()[0]
        with patch.object(mock_store, 'MAX_OPERATORS', operator_count):
            self.store.save_operator('alpha', {'business': 'sms', 'mcc': '289', 'mnc': '88', 'name': '模拟更新'})
            with self.assertRaises(ValueError):
                self.store.save_operator('alpha', {'business': 'sms', 'mcc': '123', 'mnc': '01', 'name': '模拟超额'})
        self.store.save_operator('alpha', {'business': 'sms', 'mcc': '123', 'mnc': '01', 'name': '模拟新增'})
        with patch.object(mock_store, 'MAX_RATES', rate_count):
            with self.assertRaises(ValueError):
                self.import_rows([quote(mcc='123', mnc='01')])
        self.import_rows([quote()])
        with patch.object(mock_store, 'MAX_RECEIPTS', 1):
            with self.assertRaises(ValueError):
                self.import_rows([quote()])

    def test_workspace_limit_and_last_seen_expiry_cleanup(self):
        with patch.object(mock_store, 'MAX_WORKSPACES', 2):
            self.store.list_suppliers('alpha')
            self.store.list_suppliers('beta')
            with self.assertRaises(ValueError):
                self.store.list_suppliers('gamma')
            expired = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            with self.store.connection() as connection:
                connection.execute('UPDATE workspaces SET last_seen=? WHERE id=?', (expired, 'alpha'))
            self.store.list_suppliers('gamma')
            with self.store.connection() as connection:
                for table in ('suppliers', 'operators', 'rates', 'receipts'):
                    self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table} WHERE workspace=?', ('alpha',)).fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0], 2)

    def test_shared_database_concurrency_and_secret_creation(self):
        def write(index):
            store = MockStore(self.temporary.name)
            return store.import_rows('shared', 'standard-sms', 'sms', [quote()], False)['receipt_id']

        with ThreadPoolExecutor(max_workers=6) as executor:
            identifiers = list(executor.map(write, range(12)))
        self.assertEqual(len(set(identifiers)), 12)
        self.assertEqual(len(self.store.receipts('shared')), 12)
        secret_dir = Path(self.temporary.name) / 'secret-race'
        secret_dir.mkdir()
        with ThreadPoolExecutor(max_workers=6) as executor:
            values = list(executor.map(lambda index: persistent_secret(secret_dir), range(12)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual([path.name for path in secret_dir.iterdir()], ['.session-key'])


if __name__ == '__main__':
    unittest.main()
