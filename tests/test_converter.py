from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import os
import sys
import unittest
import zipfile
import re

import openpyxl
import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import app
from converter import HEADERS, analyze, convert, read_source


def fixture(rows, shifted=False):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'Pricing_'
    prefix = [None] if shifted else []
    sheet.append(prefix + ['MCC', 'MNC', 'New Price (EUR/100)', 'Price Valid from'])
    for row in rows:
        sheet.append(prefix + row)
    for row in sheet.iter_rows(min_row=2):
        row[1 + int(shifted)].number_format = '00'
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def options(**overrides):
    return {'confirmed': 'true', 'target_unit': 'per1', 'upstream_rate': '0', 'missing_date_policy': 'skip', 'duplicate_policy': 'skip'} | overrides


def rewrite_archive(payload, name, transform):
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(payload)) as source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for member in source.infolist():
            content = source.read(member)
            target.writestr(member.filename, transform(content) if member.filename == name else content)
    return output.getvalue()


class ConversionTests(unittest.TestCase):
    def test_real_biff8_layout_units_dates_and_leading_zeros(self):
        output, counts = convert(fixture([[412, 1, 8.8413, '2026-12-01 08:00']]), options())
        self.assertEqual(output[:8], bytes.fromhex('d0cf11e0a1b11ae1'))
        workbook = xlrd.open_workbook(file_contents=output)
        sheet = workbook.sheet_by_name('Sheet1')
        self.assertEqual(sheet.row_values(0), HEADERS)
        self.assertEqual(sheet.row_values(1)[:2], ['412', '01'])
        self.assertAlmostEqual(sheet.cell_value(1, 2), 0.088413, places=12)
        self.assertEqual(sheet.cell_value(1, 3), 0)
        self.assertEqual(xlrd.xldate_as_datetime(sheet.cell_value(1, 4), workbook.datemode), datetime(2026, 12, 1, 8))
        self.assertEqual(counts['exported'], 1)

    def test_per100_and_explicit_upstream_rate(self):
        output, _ = convert(fixture([[289, 88, 8.8413, '2026-12-01']]), options(target_unit='per100', upstream_rate='0.0123'))
        sheet = xlrd.open_workbook(file_contents=output).sheet_by_index(0)
        self.assertEqual(sheet.cell_value(1, 2), 8.8413)
        self.assertEqual(sheet.cell_value(1, 3), 0.0123)

    def test_skip_fill_and_keep_missing_date(self):
        payload = fixture([[289, 88, 1, None], [289, 67, 2, '2026-12-01']])
        _, counts = convert(payload, options())
        self.assertEqual((counts['exported'], counts['skipped']), (1, 1))
        filled, counts = convert(payload, options(missing_date_policy='fill', fallback_date='2026-11-03T09:00'))
        self.assertEqual(counts['exported'], 2)
        workbook = xlrd.open_workbook(file_contents=filled)
        self.assertEqual(xlrd.xldate_as_datetime(workbook.sheet_by_index(0).cell_value(1, 4), workbook.datemode), datetime(2026, 11, 3, 9))
        kept, _ = convert(payload, options(missing_date_policy='keep'))
        self.assertEqual(xlrd.open_workbook(file_contents=kept).sheet_by_index(0).cell_value(1, 4), '')

    def test_duplicate_conflicts_not_silently_overwritten(self):
        payload = fixture([[289, 88, 1, '2026-12-01'], [289, 88, 2, '2026-12-01'], [289, 67, 3, '2026-12-01']])
        self.assertEqual(analyze(payload, 'sample.xlsx')['stats']['duplicate_rows'], 2)
        _, counts = convert(payload, options())
        self.assertEqual((counts['exported'], counts['skipped']), (1, 2))
        output, counts = convert(payload, options(duplicate_policy='keep'))
        self.assertEqual(counts['exported'], 3)
        self.assertEqual(xlrd.open_workbook(file_contents=output).sheet_by_index(0).nrows, 4)

    def test_invalid_codes_prices_dates_and_formulas_not_exported(self):
        payload = fixture([[289, 'N/D', 1, '2026-12-01'], [289, 88, -1, '2026-12-01'], [289, 67, '=1+1', '2026-12-01'], [289, 80, 2, 'bad-date'], [289, 81, 3, '2026-12-01']])
        analysis = analyze(payload, 'sample.xlsx')
        self.assertEqual(analysis['stats']['invalid_rows'], 4)
        self.assertEqual(analysis['stats']['missing_dates'], 0)
        _, counts = convert(payload, options())
        self.assertEqual((counts['exported'], counts['skipped']), (1, 4))

    def test_confirmation_and_policies_required(self):
        payload = fixture([[289, 88, 1, '2026-12-01']])
        for name, invalid in [('confirmed', 'false'), ('target_unit', ''), ('upstream_rate', 'NaN'), ('missing_date_policy', ''), ('duplicate_policy', ''), ('upstream_rate', '1e-1000000')]:
            with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                convert(payload, options(**{name: invalid}))
        with self.assertRaises(ValueError):
            convert(payload, options(missing_date_policy='fill'))

    def test_shifted_header_with_empty_first_column(self):
        _, _, records = read_source(fixture([[289, 88, 1, '2026-12-01']], shifted=True))
        self.assertEqual(records[0]['source_row'], 2)
        self.assertEqual(records[0]['mnc'], '88')

    def test_wrong_or_missing_dimension_does_not_drop_rows(self):
        payload = fixture([[289, 88, 1, '2026-12-01'], [289, 67, 2, '2026-12-01']])
        for replacement in [b'<dimension ref="A1:D2"/>', b'']:
            altered = rewrite_archive(payload, 'xl/worksheets/sheet1.xml', lambda data: re.sub(rb'<dimension[^>]*/>', replacement, data))
            self.assertEqual(analyze(altered, 'sample.xlsx')['stats']['total_rows'], 2)

    def test_sectioned_number_format_preserves_mnc_zeros(self):
        workbook = openpyxl.load_workbook(BytesIO(fixture([[289, 1, 1, '2026-12-01']])))
        workbook.active['B2'].number_format = '000;[Red]-000'
        output = BytesIO()
        workbook.save(output)
        converted, _ = convert(output.getvalue(), options())
        self.assertEqual(xlrd.open_workbook(file_contents=converted).sheet_by_index(0).cell_value(1, 1), '001')
        workbook.active['B2'].number_format = '[>=100]000;00'
        output = BytesIO()
        workbook.save(output)
        self.assertEqual(analyze(output.getvalue(), 'sample.xlsx')['stats']['invalid_rows'], 1)

    def test_zero_uses_zero_number_format_section(self):
        workbook = openpyxl.load_workbook(BytesIO(fixture([[289, 0, 1, '2026-12-01']])))
        workbook.active['B2'].number_format = '000;[Red]-000;00'
        output = BytesIO()
        workbook.save(output)
        converted, _ = convert(output.getvalue(), options())
        self.assertEqual(xlrd.open_workbook(file_contents=converted).sheet_by_index(0).cell_value(1, 1), '00')

    def test_noncanonical_sheet_or_shared_strings_are_rejected(self):
        payload = fixture([[289, 88, 1, '2026-12-01']])
        output = BytesIO()
        with zipfile.ZipFile(BytesIO(payload)) as source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
            for member in source.infolist():
                content = source.read(member).replace(b'/xl/worksheets/sheet1.xml', b'/xl/other/sheet1.xml')
                name = member.filename.replace('xl/worksheets/sheet1.xml', 'xl/other/sheet1.xml')
                target.writestr(name, content)
        with self.assertRaisesRegex(ValueError, '部件路径'):
            analyze(output.getvalue(), 'sample.xlsx')
        shared_type = b'<Override PartName="/xl/otherStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        altered = rewrite_archive(payload, '[Content_Types].xml', lambda content: content.replace(b'</Types>', shared_type + b'</Types>'))
        with self.assertRaisesRegex(ValueError, '部件路径'):
            analyze(altered, 'sample.xlsx')

    def test_duplicate_critical_headers_rejected(self):
        workbook = openpyxl.load_workbook(BytesIO(fixture([[289, 88, 8, '2026-12-01']])))
        workbook.active['E1'] = 'New Price (EUR/100)'
        workbook.active['E2'] = 900
        output = BytesIO()
        workbook.save(output)
        with self.assertRaisesRegex(ValueError, '表头重复'):
            analyze(output.getvalue(), 'sample.xlsx')

    def test_large_shared_string_table_rejected_before_loading(self):
        payload = fixture([[289, 88, 1, '2026-12-01']])
        for content in [b'<sst>' + b'<si><t>x</t></si>' * 50001 + b'</sst>', b'<sst>' + b'x' * (4 * 1024 * 1024) + b'</sst>']:
            output = BytesIO()
            with zipfile.ZipFile(BytesIO(payload)) as source, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
                for member in source.infolist():
                    target.writestr(member.filename, source.read(member))
                target.writestr('xl/sharedStrings.xml', content)
            with self.assertRaisesRegex(ValueError, '共享字符串'):
                analyze(output.getvalue(), 'sample.xlsx')

    def test_malformed_data_rows_return_json_400(self):
        payload = fixture([[289, 88, 1, '2026-12-01']])
        altered = rewrite_archive(payload, 'xl/worksheets/sheet1.xml', lambda data: data.replace(b'</row>', b'</bad-row>', 1))
        response = app.test_client().post('/api/analyze', data={'file': (BytesIO(altered), 'broken.xlsx')})
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json)

    def test_invalid_archive_and_decompression_limit(self):
        with self.assertRaises(ValueError):
            read_source(b'not an xlsx')
        archive = BytesIO()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr('oversized.xml', b'0' * (64 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, '64MB'):
            read_source(archive.getvalue())

    def test_full_private_sample_and_original_template(self):
        source_path = os.environ.get('VIV_PRIVATE_SOURCE')
        template_path = os.environ.get('VIV_PRIVATE_TEMPLATE')
        if not source_path or not template_path:
            self.skipTest('set VIV_PRIVATE_SOURCE and VIV_PRIVATE_TEMPLATE for authorized private fixtures')
        payload = Path(source_path).read_bytes()
        analysis = analyze(payload, 'sample.xlsx')
        self.assertEqual(analysis['stats']['total_rows'], 2591)
        self.assertEqual(analysis['stats']['missing_dates'], 753)
        output, counts = convert(payload, options(target_unit='per100', missing_date_policy='keep', duplicate_policy='keep'))
        target = xlrd.open_workbook(template_path).sheet_by_index(0)
        actual = xlrd.open_workbook(file_contents=output).sheet_by_index(0)
        self.assertEqual(actual.name, target.name)
        self.assertEqual(actual.row_values(0), target.row_values(0))
        self.assertEqual(actual.ncols, target.ncols)
        self.assertEqual(counts['exported'] + counts['skipped'], 2591)
        _, _, source_rows = read_source(payload)
        output_row = 1
        for source in source_rows:
            if source['errors']:
                continue
            self.assertEqual(actual.cell_value(output_row, 0), source['mcc'])
            self.assertEqual(actual.cell_value(output_row, 1), source['mnc'])
            self.assertLess(abs(Decimal(str(actual.cell_value(output_row, 2))) - source['new_price']), Decimal('0.000000001'))
            output_row += 1
        self.assertEqual(output_row, actual.nrows)
        strict, strict_counts = convert(payload, options())
        self.assertGreater(strict_counts['exported'], 0)
        self.assertEqual(xlrd.open_workbook(file_contents=strict).sheet_by_index(0).nrows, strict_counts['exported'] + 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.payload = fixture([[412, 1, 8.8413, '2026-12-01 08:00']])

    def test_analyze_and_download(self):
        response = self.client.post('/api/analyze', data={'file': (BytesIO(self.payload), 'sample.xlsx')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['stats']['total_rows'], 1)
        response = self.client.post('/api/convert', data=options() | {'file': (BytesIO(self.payload), 'sample.xlsx')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Exported-Rows'], '1')
        self.assertIn('Template_sendRate_converted.xls', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_bad_uploads_and_missing_confirmation(self):
        for data in [{}, {'file': (BytesIO(b''), 'empty.xlsx')}, {'file': (BytesIO(b'wrong'), 'data.exe')}, {'file': (BytesIO(b'wrong'), 'data.xlsx')}]:
            self.assertEqual(self.client.post('/api/analyze', data=data).status_code, 400)
        self.assertEqual(self.client.post('/api/convert', data={'file': (BytesIO(self.payload), 'sample.xlsx')}).status_code, 400)

    def test_oversized_upload(self):
        response = self.client.post('/api/analyze', data=b'0' * (10 * 1024 * 1024 + 1), content_type='application/octet-stream')
        self.assertEqual(response.status_code, 413)
        self.assertIn('10MB', response.json['error'])

    def test_multipart_upload_larger_than_parser_buffer(self):
        workbook = openpyxl.load_workbook(BytesIO(self.payload))
        for row_number in range(3, 3500):
            workbook.active.cell(row_number, 1, row_number)
            workbook.active.cell(row_number, 2, 1)
            workbook.active.cell(row_number, 3, 1)
            workbook.active.cell(row_number, 4, '2026-12-01')
            workbook.active.cell(row_number, 5, str(row_number) * 10)
        output = BytesIO()
        workbook.save(output)
        self.assertGreater(len(output.getvalue()), 65536)
        response = self.client.post('/api/analyze', data={'file': (BytesIO(output.getvalue()), 'larger.xlsx')})
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
