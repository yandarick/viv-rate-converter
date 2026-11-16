import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import requests
import xlrd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from converter import HEADERS, analyze, convert, read_source


def verify(base_url, source, template, output_directory):
    payload = source.read_bytes()
    client = requests.Session()
    client.trust_env = False
    health = client.get(base_url + '/health', timeout=20)
    health.raise_for_status()
    assert health.json()['status'] == 'ok'
    page = client.get(base_url + '/', timeout=20)
    page.raise_for_status()
    assert '影核' in page.text and '/static/app.js' in page.text
    for name in ['app.js', 'style.css']:
        response = client.get(base_url + '/static/' + name, timeout=20)
        response.raise_for_status()
        assert response.content == (Path(__file__).resolve().parents[1] / 'static' / name).read_bytes()
    response = client.post(base_url + '/api/analyze', files={'file': (source.name, payload)}, timeout=40)
    response.raise_for_status()
    remote_analysis = response.json()
    assert remote_analysis['stats'] == analyze(payload, source.name)['stats']
    target = xlrd.open_workbook(template).sheet_by_index(0)
    _, _, records = read_source(payload)
    outputs = []
    for unit, policy in [('per100', 'keep'), ('per1', 'skip')]:
        rules = dict(confirmed='true', target_unit=unit, upstream_rate='0', missing_date_policy=policy, duplicate_policy=policy)
        response = client.post(base_url + '/api/convert', files={'file': (source.name, payload)}, data=rules, timeout=40)
        response.raise_for_status()
        assert response.content[:8] == bytes.fromhex('d0cf11e0a1b11ae1')
        workbook = xlrd.open_workbook(file_contents=response.content)
        sheet = workbook.sheet_by_index(0)
        assert sheet.row_values(0) == target.row_values(0) == HEADERS
        assert sheet.name == target.name and sheet.ncols == 5
        _, expected = convert(payload, rules)
        assert int(response.headers['X-Exported-Rows']) == expected['exported']
        assert int(response.headers['X-Skipped-Rows']) == expected['skipped']
        assert sheet.nrows == expected['exported'] + 1
        output_row = 1
        for record in records:
            if record['errors'] or (policy == 'skip' and (record['duplicate'] or record['effective_time'] is None)):
                continue
            assert sheet.cell_value(output_row, 0) == record['mcc']
            assert sheet.cell_value(output_row, 1) == record['mnc']
            expected_price = record['new_price'] / Decimal(100) if unit == 'per1' else record['new_price']
            assert abs(Decimal(str(sheet.cell_value(output_row, 2))) - expected_price) < Decimal('0.000000001')
            assert sheet.cell_value(output_row, 3) == 0
            if record['effective_time'] is None:
                assert sheet.cell_value(output_row, 4) == ''
            else:
                assert xlrd.xldate_as_datetime(sheet.cell_value(output_row, 4), workbook.datemode) == record['effective_time']
            output_row += 1
        assert output_row == sheet.nrows
        label = '公网' if base_url.startswith('https://') else '内网'
        destination = output_directory / f'Template_sendRate_{label}_{unit}_{policy}_仅格式测试.xls'
        destination.write_bytes(response.content)
        outputs.append({'file': destination.name, 'sha256': hashlib.sha256(response.content).hexdigest(), 'bytes': len(response.content), 'stats': expected})
    rejected = client.post(base_url + '/api/convert', files={'file': ('bad.xlsx', b'broken')}, timeout=20)
    assert rejected.status_code == 400 and 'error' in rejected.json()
    return {'base_url': base_url, 'analysis': remote_analysis['stats'], 'outputs': outputs, 'malformed_upload': 400, 'checks': '健康、页面、静态资源一致性、上传分析、两种转换、逐行编码金额日期、原模板结构、错误请求均通过'}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True, action='append', help='Explicit deployment URL; repeat to check more than one')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    arguments = parser.parse_args()
    arguments.out.mkdir(parents=True, exist_ok=True)
    results = [verify(url, arguments.source, arguments.template, arguments.out) for url in arguments.base]
    report = {'checked_at': datetime.now(ZoneInfo('America/Denver')).isoformat(), 'source_md5': hashlib.md5(arguments.source.read_bytes()).hexdigest(), 'template_md5': hashlib.md5(arguments.template.read_bytes()).hexdigest(), 'results': results}
    (arguments.out / '部署验收结果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
