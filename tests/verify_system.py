import argparse
from datetime import datetime
from decimal import Decimal
from http.cookiejar import MozillaCookieJar
from io import BytesIO
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import requests
import xlrd


class RateAwareSession(requests.Session):
    def request(self, *arguments, **options):
        for attempt in range(6):
            response = super().request(*arguments, **options)
            if response.status_code != 429 or attempt == 5:
                return response
            time.sleep(1.25)


def checked(response, code=200):
    if response.status_code != code:
        raise AssertionError(f'{response.request.method} {response.url}: {response.status_code} {response.text[:400]}')
    return response


def post_file(session, base, endpoint, payload, options, name='demo.xls'):
    return session.post(base + endpoint, data=options, files={'file': (name, payload, 'application/vnd.ms-excel')}, timeout=60)


def verify_rows(payload, preview):
    assert payload.startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
    book = xlrd.open_workbook(file_contents=payload, formatting_info=True)
    sheet = book.sheet_by_index(0)
    expected_headers = ['CC', 'MNC', '呼出费率', '呼入费率', '生效时间'] if preview['business'] == 'voice' else ['MCC', 'MNC', '下行费率', '上行费率', '生效时间']
    assert sheet.name == 'Sheet1' and sheet.row_values(0) == expected_headers
    expected = [row for row in preview['rows'] if row['eligible']]
    assert sheet.nrows == len(expected) + 1
    for index, row in enumerate(expected, 1):
        assert sheet.cell_value(index, 0) == row['mcc']
        assert sheet.cell_value(index, 1) == row['mnc']
        assert sheet.cell_type(index, 1) == xlrd.XL_CELL_TEXT
        assert sheet.cell_value(index, 2) == float(Decimal(row['downstream']))
        assert sheet.cell_value(index, 3) == float(Decimal(row['upstream']))
        actual_date = xlrd.xldate_as_datetime(sheet.cell_value(index, 4), book.datemode)
        assert actual_date == datetime.fromisoformat(row['effective_time'])
        if row['price_increased']:
            style = book.xf_list[sheet.cell_xf_index(index, 2)]
            assert style.background.fill_pattern == 1
    return len(expected)


def exercise(session, base, example, supplier, output, direction='both'):
    payload = checked(session.get(base + '/api/examples/' + example, timeout=20)).content
    options = {'supplier_id': supplier, 'direction': direction}
    preview = checked(post_file(session, base, '/api/workflow/preview', payload, options)).json()
    assert preview['stats']['exported'] > 0 and preview['stats']['blocked'] == 0
    if preview['stats']['price_increases']:
        checked(post_file(session, base, '/api/workflow/export', payload, options | {'confirmed': 'true'}), 400)
    exported = checked(post_file(session, base, '/api/workflow/export', payload, options | {'confirmed': 'true', 'confirm_increases': 'true', 'allow_partial': 'true'}))
    count = verify_rows(exported.content, preview)
    proof = exported.headers['X-Export-Proof']
    checked(post_file(session, base, '/api/workflow/import', exported.content, options | {'confirmed': 'true'}), 400)
    receipt = checked(post_file(session, base, '/api/workflow/import', exported.content, options | {'confirmed': 'true', 'confirm_increases': 'true', 'export_proof': proof})).json()
    assert receipt['mode'] == 'mock' and receipt['imported'] == count
    after = checked(post_file(session, base, '/api/workflow/preview', payload, options)).json()
    assert after['stats']['price_increases'] == 0
    output.mkdir(parents=True, exist_ok=True)
    (output / (example + '_仅模拟数据.xls')).write_bytes(exported.content)
    return {'example': example, 'stats': preview['stats'], 'receipt': receipt, 'rows_verified': count}


def verify(base, output, state_path):
    session = RateAwareSession()
    session.cookies = MozillaCookieJar(str(state_path.with_suffix('.cookies')))
    health = checked(session.get(base + '/health', timeout=20)).json()
    assert health['version'] == '2026.11.12'
    suppliers = checked(session.get(base + '/api/suppliers', timeout=20)).json()
    assert suppliers['mode'] == 'mock'
    supplier_ids = {item['id'] for item in suppliers['suppliers']}
    assert {'standard-sms', 'skyline-sms', 'voice-demo', 'tencent-sms', 'volcano-sms'} <= supplier_ids
    sms = exercise(session, base, 'sms_both', 'standard-sms', output)
    voice = exercise(session, base, 'voice', 'voice-demo', output)
    payload = checked(session.get(base + '/api/examples/sms_both', timeout=20)).content
    email_payload = checked(session.get(base + '/api/examples/email.eml', timeout=20)).content
    options = {'supplier_id': 'standard-sms', 'direction': 'both'}
    first = checked(session.post(base + '/api/workflow/preview', data=options, files={'file': ('demo.xls', payload), 'email_file': ('demo.eml', email_payload)}, timeout=30)).json()
    assert first['stats']['blocked'] > 0 and first['email']['date_candidates']
    chosen = first['email']['date_candidates'][0]['value']
    second = checked(session.post(base + '/api/workflow/preview', data=options | {'email_date': chosen}, files={'file': ('demo.xls', payload), 'email_file': ('demo.eml', email_payload)}, timeout=30)).json()
    assert second['stats']['blocked'] == 0
    assert any('邮件' in row['date_source'] for row in second['rows'])
    checked(post_file(session, base, '/api/workflow/preview', b'bad file', options, 'bad.xlsx'), 400)
    queried = checked(session.post(base + '/api/mock/query', json={'supplier_id': 'standard-sms', 'keys': ['460:00']}, timeout=20)).json()
    assert '460:00' in queried['rates']
    receipts = checked(session.get(base + '/api/mock/receipts', timeout=20)).json()['receipts']
    assert any(item['receipt_id'] == sms['receipt']['receipt_id'] for item in receipts)
    stranger = RateAwareSession()
    assert not checked(stranger.get(base + '/api/mock/receipts', timeout=20)).json()['receipts']
    state = {'base': base, 'receipt_id': sms['receipt']['receipt_id'], 'cookie_file': str(state_path.with_suffix('.cookies'))}
    session.cookies.save(ignore_discard=True, ignore_expires=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    report = {'checked_at': datetime.now(ZoneInfo('America/Denver')).isoformat(), 'base': base, 'health': health,
              'suppliers': sorted(supplier_ids), 'sms': sms, 'voice': voice, 'email_date_selection': True,
              'bad_file_rejected': True, 'price_increase_confirmation': True, 'export_proof_checked': True,
              'workspace_isolation': True, 'mock_query': True, 'real_internal_system_connected': False}
    (output / '完整系统验收.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def persistence(state_path):
    state = json.loads(state_path.read_text(encoding='utf-8'))
    session = RateAwareSession()
    session.cookies = MozillaCookieJar(state['cookie_file'])
    session.cookies.load(ignore_discard=True, ignore_expires=True)
    receipts = checked(session.get(state['base'] + '/api/mock/receipts', timeout=20)).json()['receipts']
    assert any(item['receipt_id'] == state['receipt_id'] for item in receipts)
    print(json.dumps({'base': state['base'], 'persistence_after_restart': True, 'receipt_id': state['receipt_id']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:18041')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--check-persistence', action='store_true')
    arguments = parser.parse_args()
    arguments.state.parent.mkdir(parents=True, exist_ok=True)
    if arguments.check_persistence:
        persistence(arguments.state)
    else:
        verify(arguments.base.rstrip('/'), arguments.output, arguments.state)
