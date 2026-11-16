from datetime import datetime
from io import BytesIO
from pathlib import Path
import sys
import unittest

import xlrd
import xlwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mail_dates import extract_dates, mail_context, parse_time
from quote_reader import read_quotes
from workflow import build_preview, export_preview


def profile(**overrides):
    return {'id': 'demo', 'name': '模拟供应商', 'business': 'sms', 'timezone': 'UTC', 'currency': 'EUR', 'divisor': '1',
            'headers': {'mcc': 'MCC', 'mnc': 'MNC', 'mt': 'New Price', 'mo': 'MO', 'date': 'Effective Date'},
            'missing_date_policy': 'email_then_system', 'invalid_code_policy': 'max', 'fallback_mnc': '999',
            'contract_rate': '5', 'contract_currency': 'USD', 'contract_fx_rate': ''} | overrides


def quote_file(rows, headers=None):
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('Pricing')
    for column, value in enumerate(headers or ['MCC', 'MNC', 'New Price', 'MO', 'Effective Date']):
        sheet.write(0, column, value)
    for number, values in enumerate(rows, 1):
        for column, value in enumerate(values):
            if value is not None:
                style = xlwt.easyxf(num_format_str='yyyy-mm-dd hh:mm:ss') if isinstance(value, datetime) else xlwt.Style.default_style
                sheet.write(number, column, value, style)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class FakeStore:
    def operators(self, workspace, business):
        return [{'mcc': '289', 'mnc': value} for value in ['88', '99', '999']]

    def query_rates(self, workspace, supplier, business, keys):
        return {'289:88': {'downstream': '0.02', 'upstream': '0.001', 'currency': 'EUR', 'effective_time': '2026-11-01 08:00:00'},
                '289:999': {'downstream': '0.03', 'upstream': '0.002', 'currency': 'EUR', 'effective_time': '2026-11-01 08:00:00'}}


def preview(rows, supplier=None, direction='both', options=None, mail=None):
    return build_preview(quote_file(rows), 'sample.xls', supplier or profile(), direction, options or {}, FakeStore(), 'test', mail)


class WorkflowTests(unittest.TestCase):
    def test_price_increase_and_system_date_are_traceable(self):
        result = preview([['289', '88', 0.025, 0.002, None]])
        self.assertEqual(result['stats']['exported'], 1)
        row = result['rows'][0]
        self.assertTrue(row['price_increased'])
        self.assertEqual(row['effective_time'], '2026-11-01 08:00:00')
        self.assertIn('模拟系统', row['date_source'])
        with self.assertRaisesRegex(ValueError, '上涨'):
            export_preview(result, {'confirmed': 'true'})
        payload = export_preview(result, {'confirmed': 'true', 'confirm_increases': 'true'})
        sheet = xlrd.open_workbook(file_contents=payload, formatting_info=True).sheet_by_index(0)
        self.assertEqual(sheet.row_values(0), ['MCC', 'MNC', '下行费率', '上行费率', '生效时间'])
        self.assertEqual(sheet.cell_value(1, 1), '88')
        self.assertAlmostEqual(sheet.cell_value(1, 2), 0.025)

    def test_email_selection_overrides_missing_date_not_existing_date(self):
        mail = mail_context('Effective date: 2026-11-15 23:30 UTC')
        result = preview([['289', '88', 0.02, 0.001, None]], mail=mail)
        self.assertEqual(result['stats']['blocked'], 1)
        chosen = mail['date_candidates'][0]['value']
        result = preview([['289', '88', 0.02, 0.001, None]], options={'email_date': chosen}, mail=mail)
        self.assertEqual(result['rows'][0]['effective_time'], '2026-11-16 07:30:00')
        result = preview([['289', '88', 0.02, 0.001, datetime(2026, 12, 1, 8)]], options={'email_date': chosen}, mail=mail)
        self.assertEqual(result['rows'][0]['effective_time'], '2026-12-01 16:00:00')

    def test_non_numeric_mnc_takes_separate_direction_maxima(self):
        result = preview([['289', '88', 0.02, 0.004, datetime(2026, 11, 1)], ['289', '99', 0.04, 0.001, datetime(2026, 11, 1)],
                          ['289', 'N/D', 0.01, 0.001, datetime(2026, 11, 1)]])
        row = result['rows'][2]
        self.assertEqual((row['mcc'], row['mnc']), ('289', '999'))
        self.assertEqual((row['downstream'], row['upstream']), ('0.04', '0.004'))
        self.assertTrue(row['eligible'])

    def test_unknown_numeric_code_uses_same_configured_fallback(self):
        result = preview([['289', '88', 0.02, 0.001, datetime(2026, 11, 1)], ['289', '77', 0.03, 0.002, datetime(2026, 11, 1)]])
        self.assertEqual(result['rows'][1]['mnc'], '999')
        self.assertEqual(result['stats']['invalid_codes'], 1)

    def test_contract_currency_is_never_silently_mixed(self):
        rows = [['289', 'N/D', 0.02, 0.001, datetime(2026, 11, 1)]]
        result = preview(rows, supplier=profile(invalid_code_policy='contract'))
        self.assertEqual(result['stats']['blocked'], 1)
        self.assertIn('币种', ' '.join(result['rows'][0]['errors']))
        result = preview(rows, supplier=profile(invalid_code_policy='contract', contract_fx_rate='0.9'))
        self.assertEqual(result['rows'][0]['downstream'], '4.5')
        self.assertEqual(result['rows'][0]['upstream'], '4.5')

    def test_conflicting_duplicates_are_blocked_and_identical_merge(self):
        base = ['289', '88', 0.02, 0.001, datetime(2026, 11, 1)]
        result = preview([base, base])
        self.assertEqual((result['stats']['exported'], result['stats']['merged']), (1, 1))
        result = preview([base, ['289', '88', 0.03, 0.001, datetime(2026, 11, 1)]])
        self.assertEqual(result['stats']['blocked'], 2)
        with self.assertRaisesRegex(ValueError, '阻断'):
            export_preview(result, {'confirmed': 'true'})

    def test_partial_export_requires_confirmation(self):
        result = preview([['289', '88', 0.02, 0.001, datetime(2026, 11, 1)], ['ABC', '88', 0.02, 0.001, datetime(2026, 11, 1)]])
        with self.assertRaisesRegex(ValueError, '阻断'):
            export_preview(result, {'confirmed': 'true'})
        output = export_preview(result, {'confirmed': 'true', 'allow_partial': 'true'})
        self.assertEqual(xlrd.open_workbook(file_contents=output).sheet_by_index(0).nrows, 2)

    def test_divisor_and_missing_mo(self):
        payload = quote_file([['289', '88', 2, datetime(2026, 11, 1)]], ['MCC', 'MNC', 'New Price', 'Effective Date'])
        result = build_preview(payload, 'sample.xls', profile(divisor='100', headers={'mcc': 'MCC', 'mnc': 'MNC', 'mt': 'New Price', 'mo': '', 'date': 'Effective Date'}), 'both', {}, FakeStore(), 'test')
        self.assertEqual((result['rows'][0]['downstream'], result['rows'][0]['upstream']), ('0.02', '0'))

    def test_mo_only_preserves_other_direction(self):
        result = preview([['289', '88', None, 0.002, datetime(2026, 11, 1)]], direction='mo')
        self.assertEqual(result['rows'][0]['downstream'], '0.02')
        result = preview([['289', '99', None, 0.002, datetime(2026, 11, 1)]], direction='mo')
        self.assertEqual(result['stats']['blocked'], 1)

    def test_mo_fallback_never_changes_other_direction(self):
        result = preview([['289', '88', None, 0.003, datetime(2026, 11, 1)], ['289', 'N/D', None, 0.001, datetime(2026, 11, 1)]], direction='mo')
        row = result['rows'][1]
        self.assertEqual((row['mnc'], row['downstream'], row['upstream']), ('999', '0.03', '0.003'))

    def test_configured_mo_header_cannot_be_silently_replaced_with_zero(self):
        payload = quote_file([['289', '88', 0.02, 0.009, datetime(2026, 11, 1)]], ['MCC', 'MNC', 'New Price', 'M0', 'Effective Date'])
        with self.assertRaisesRegex(ValueError, 'MO'):
            read_quotes(payload, 'sample.xls', profile(), 'both')

    def test_sparse_xls_does_not_materialize_a_full_rectangle(self):
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet('Pricing')
        sheet.write(19999, 63, 'sparse')
        output = BytesIO()
        workbook.save(output)
        from quote_reader import xls_sheets
        sheets = xls_sheets(output.getvalue())
        _, rows = next(sheets)
        self.assertNotIsInstance(rows, list)
        self.assertEqual(next(rows), [])
        sheets.close()

    def test_formula_in_xls_is_rejected(self):
        payload = quote_file([['289', '88', xlwt.Formula('1+1'), 0, datetime(2026, 11, 1)]])
        with self.assertRaisesRegex(ValueError, '公式'):
            read_quotes(payload, 'sample.xls', profile(), 'both')

    def test_mail_header_date_is_not_an_effective_date(self):
        raw = b'Date: Thu, 12 Nov 2026 09:00:00 +0000\r\nSubject: Rates\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nEffective date: 2026-11-20 00:00 GMT\r\n'
        result = mail_context(payload=raw, filename='rates.eml')
        self.assertEqual(len(result['date_candidates']), 1)
        self.assertIn('2026-11-20', result['date_candidates'][0]['value'])

    def test_dst_ambiguity_and_nonexistent_times_require_offset(self):
        for value in ['2026-10-25 01:30:00', '2026-03-29 01:30:00']:
            with self.assertRaisesRegex(ValueError, '夏令时'):
                parse_time(value, 'Europe/London')
        self.assertEqual(parse_time('2026-10-25T01:30:00+01:00', 'Europe/London'), datetime(2026, 10, 25, 8, 30))

    def test_multiple_and_invalid_email_dates(self):
        candidates = extract_dates('old 2026-11-01; new 2026-11-20; invalid 2026-13-40')
        self.assertEqual(len(candidates), 2)
        with self.assertRaisesRegex(ValueError, '候选'):
            preview([['289', '88', 0.02, 0, None]], options={'email_date': '2026-11-21'}, mail=mail_context('2026-11-20'))

    def test_mail_explicit_offsets_are_not_dropped(self):
        for text in ['20 Nov 2026 00:00 GMT+8', '2026-11-20 00:00+0800', '2026-11-20 00:00 UTC+08:00']:
            candidate = extract_dates(text)[0]
            self.assertEqual(parse_time(candidate['value'], 'UTC'), datetime(2026, 11, 20))
        with self.assertRaisesRegex(ValueError, '时差'):
            extract_dates('2026-11-20 00:00 UTC+25:00')
        with self.assertRaisesRegex(ValueError, '时区'):
            extract_dates('2026-11-20 00:00 CST')

    def test_combined_mccmnc_keeps_leading_zero_mnc(self):
        supplier = profile(headers={'mcc': '', 'mnc': '', 'mccmnc': 'MCCMNC', 'mt': 'Price', 'mo': '', 'date': ''})
        payload = quote_file([['289088', 0.02]], ['MCCMNC', 'Price'])
        _, _, rows = read_quotes(payload, 'sample.xls', supplier, 'mt')
        self.assertEqual((rows[0]['mcc'], rows[0]['mnc']), ('289', '088'))

    def test_country_mapping_is_explicit_and_unmapped_country_is_blocked(self):
        supplier = profile(headers={'mcc': '', 'mnc': '', 'country': 'Code', 'mt': 'Price', 'mo': '', 'date': ''}, country_map={'XX': {'mcc': '289', 'mnc': '88'}})
        payload = quote_file([['XX', 0.02], ['YY', 0.03]], ['Code', 'Price'])
        result = build_preview(payload, 'sample.xls', supplier, 'mt', {}, FakeStore(), 'test')
        self.assertTrue(result['rows'][0]['eligible'])
        self.assertFalse(result['rows'][1]['eligible'])
        self.assertIn('不能猜', ' '.join(result['rows'][1]['errors']))


if __name__ == '__main__':
    unittest.main()
