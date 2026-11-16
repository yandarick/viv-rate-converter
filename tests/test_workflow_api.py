from io import BytesIO
from pathlib import Path
import sys
import unittest

import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app


class FullWorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.payload = self.client.get('/api/examples/sms_both').data
        self.options = {'supplier_id': 'standard-sms', 'direction': 'both', 'sheet_name': 'Pricing_'}

    def post_file(self, path, options=None, payload=None, client=None):
        return (client or self.client).post(path, data=(options or self.options) | {'file': (BytesIO(payload or self.payload), 'sample.xls')})

    def test_preview_export_import_and_price_query(self):
        before = self.post_file('/api/workflow/preview')
        self.assertEqual(before.status_code, 200, before.json)
        self.assertGreater(before.json['stats']['price_increases'], 0)
        self.assertTrue(any(row['mnc'] == '00' for row in before.json['rows']))
        rejected = self.post_file('/api/workflow/export', self.options | {'confirmed': 'true'})
        self.assertEqual(rejected.status_code, 400)
        exported = self.post_file('/api/workflow/export', self.options | {'confirmed': 'true', 'confirm_increases': 'true', 'allow_partial': 'true'})
        self.assertEqual(exported.status_code, 200, exported.json if exported.is_json else '')
        self.assertTrue(exported.data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')))
        proof = exported.headers['X-Export-Proof']
        imported = self.post_file('/api/workflow/import', self.options | {'confirmed': 'true', 'confirm_increases': 'true', 'export_proof': proof}, exported.data)
        self.assertEqual(imported.status_code, 200, imported.json)
        self.assertEqual(imported.json['mode'], 'mock')
        self.assertEqual(imported.json['imported'], before.json['stats']['exported'])
        queried = self.client.post('/api/mock/query', json={'supplier_id': 'standard-sms', 'keys': ['460:00']})
        self.assertEqual(queried.status_code, 200, queried.json)
        receipts = self.client.get('/api/mock/receipts')
        self.assertEqual(receipts.status_code, 200)
        self.assertTrue(receipts.json['receipts'])
        after = self.post_file('/api/workflow/preview')
        self.assertEqual(after.json['stats']['price_increases'], 0)

    def test_export_proof_binds_workspace_supplier_currency_and_file(self):
        exported = self.post_file('/api/workflow/export', self.options | {'confirmed': 'true', 'confirm_increases': 'true', 'allow_partial': 'true'})
        self.assertEqual(exported.status_code, 200)
        options = self.options | {'confirmed': 'true', 'confirm_increases': 'true', 'export_proof': exported.headers['X-Export-Proof']}
        for body, client, supplier in [(exported.data + b'changed', self.client, 'standard-sms'),
                                       (exported.data, app.test_client(), 'standard-sms'),
                                       (exported.data, self.client, 'skyline-sms')]:
            result = self.post_file('/api/workflow/import', options | {'supplier_id': supplier}, body, client)
            self.assertEqual(result.status_code, 400, result.json)
        result = self.post_file('/api/workflow/import', self.options | {'confirmed': 'true'}, exported.data)
        self.assertEqual(result.status_code, 400)

    def test_email_date_requires_selection_and_survives_export(self):
        options = self.options | {'email_text': 'Effective date: 2026-11-20 23:30 UTC'}
        first = self.post_file('/api/workflow/preview', options)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertGreater(first.json['stats']['blocked'], 0)
        chosen = first.json['email']['date_candidates'][0]['value']
        second = self.post_file('/api/workflow/preview', options | {'email_date': chosen})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json['stats']['blocked'], 0)
        dates = [row['effective_time'] for row in second.json['rows'] if '邮件' in row['date_source']]
        self.assertTrue(dates)
        self.assertTrue(all(value == '2026-11-21 07:30:00' for value in dates))

    def test_voice_demo_export_and_import(self):
        payload = self.client.get('/api/examples/voice').data
        options = {'supplier_id': 'voice-demo', 'direction': 'both', 'sheet_name': 'Pricing_', 'confirmed': 'true', 'confirm_increases': 'true', 'allow_partial': 'true'}
        result = self.post_file('/api/workflow/preview', options, payload)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertGreater(result.json['stats']['exported'], 0)
        result = self.post_file('/api/workflow/export', options, payload)
        self.assertEqual(result.status_code, 200, result.json if result.is_json else '')
        sheet = xlrd.open_workbook(file_contents=result.data).sheet_by_index(0)
        self.assertEqual(sheet.row_values(0), ['CC', 'MNC', '呼出费率', '呼入费率', '生效时间'])
        imported = self.post_file('/api/workflow/import', options | {'export_proof': result.headers['X-Export-Proof']}, result.data)
        self.assertEqual(imported.status_code, 200, imported.json)
        self.assertEqual(imported.json['business'], 'voice')

    def test_gmt8_baseline_maintenance_is_not_shifted_again(self):
        maintenance = self.client.post('/api/mock/rates', json={'supplier_id': 'standard-sms', 'confirmed': True, 'confirm_increases': True, 'rows': [
            {'mcc': '289', 'mnc': '88', 'downstream': '0.02', 'upstream': '0.001', 'currency': 'EUR', 'effective_time': '2026-11-20 00:00:00'}]})
        self.assertEqual(maintenance.status_code, 200, maintenance.json)
        from test_workflow import quote_file
        payload = quote_file([['289', '88', 0.02, 0.001, None]])
        result = self.post_file('/api/workflow/preview', self.options | {'sheet_name': 'Pricing'}, payload)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['supplier']['timezone'], 'UTC')
        self.assertEqual(result.json['rows'][0]['effective_time'], '2026-11-20 00:00:00')
        self.assertIn('模拟系统', result.json['rows'][0]['date_source'])


if __name__ == '__main__':
    unittest.main()
