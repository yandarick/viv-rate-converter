from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

import openpyxl
import xlwt


HEADERS = ['MCC', 'MNC', '下行费率', '上行费率', '生效时间']
SOURCE_HEADERS = ['MCC', 'MNC', 'New Price (EUR/100)', 'Price Valid from']
MAX_ROWS = 20000


def decimal_value(value, label):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{label}必须是非负数') from None
    if not result.is_finite() or result < 0 or result > Decimal('1000000000'):
        raise ValueError(f'{label}必须是0至10亿之间的有限数值')
    if len(result.normalize().as_tuple().digits) > 15:
        raise ValueError(f'{label}有效数字不能超过15位')
    if result.as_tuple().exponent < -12:
        raise ValueError(f'{label}小数位不能超过12位')
    return result


def code_value(cell, label):
    value = cell.value
    if isinstance(value, bool) or value is None:
        raise ValueError(f'{label}为空或不是有效编码')
    if isinstance(value, (float, int)):
        number = decimal_value(value, label)
        if number != number.to_integral_value():
            raise ValueError(f'{label}不是整数编码')
        text = str(int(number))
        sections = (cell.number_format or 'General').split(';')
        if re.search(r'\[[<>=]', cell.number_format or ''):
            raise ValueError(f'{label}条件数字格式无法可靠解释，请改为文本编码')
        positive_format = sections[2] if number == 0 and len(sections) >= 3 else sections[0]
        if re.fullmatch(r'0{1,3}', positive_format):
            text = text.zfill(len(positive_format))
        elif positive_format not in ('General', '@'):
            raise ValueError(f'{label}数字格式无法可靠解释，请改为文本编码')
    else:
        text = str(value).strip()
    if not re.fullmatch(r'\d{1,3}', text, flags=re.ASCII):
        raise ValueError(f'{label}含无法映射的编码：{text[:30]}')
    return text.zfill(3) if label == 'MCC' else text


def date_value(value):
    if value is None or str(value).strip() == '':
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError('生效时间格式无法识别') from None
    else:
        raise ValueError('生效时间必须是日期或日期文本')
    if result.tzinfo is not None:
        raise ValueError('生效时间带时区，请先明确系统时区后使用无时区时间')
    if not 1900 <= result.year <= 2100:
        raise ValueError('生效时间年份超出1900至2100')
    return result


def validate_package(package):
    members = package.infolist()
    if sum(item.file_size for item in members) > 64 * 1024 * 1024:
        raise ValueError('工作簿解压后超过64MB，请拆分后上传')
    if len(members) > 1000:
        raise ValueError('工作簿内部文件过多，请另存为简单xlsx后上传')
    if len({member.filename for member in members}) != len(members):
        raise ValueError('工作簿内部文件重名，请另存为标准xlsx')
    accepted_parts = {
        'officeDocument': r'xl/workbook\.xml',
        'worksheet': r'xl/worksheets/[^/]+\.xml',
        'sharedStrings': r'xl/sharedStrings\.xml',
        'styles': r'xl/styles\.xml',
    }
    for member in members:
        if member.filename.endswith('.rels') or member.filename == '[Content_Types].xml':
            if member.file_size > 1024 * 1024:
                raise ValueError('工作簿关系或内容类型表过大')
            root = ET.fromstring(package.read(member))
            for element in root:
                if member.filename.endswith('.rels'):
                    role = element.get('Type', '').rsplit('/', 1)[-1]
                    target = element.get('Target', '')
                    parent = posixpath.dirname(posixpath.dirname(member.filename))
                    target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(parent, target))
                else:
                    content_type = element.get('ContentType', '')
                    role = next((name for name in accepted_parts if content_type.endswith('.' + name + '+xml')), '')
                    if content_type.endswith('spreadsheetml.sheet.main+xml'):
                        role = 'officeDocument'
                    target = element.get('PartName', '').lstrip('/')
                if role in accepted_parts and (element.get('TargetMode') == 'External' or not re.fullmatch(accepted_parts[role], target)):
                    raise ValueError('工作簿使用不受支持的部件路径，请用Excel另存为标准xlsx')
    for member in members:
        if member.filename == 'xl/sharedStrings.xml':
            if member.file_size > 4 * 1024 * 1024:
                raise ValueError('共享字符串表超过4MB，请删除无关数据后另存')
            count = 0
            with package.open(member) as stream:
                for _, element in ET.iterparse(stream, events=('end',)):
                    if element.tag.rsplit('}', 1)[-1] == 'si':
                        count += 1
                        if count > 50000:
                            raise ValueError('共享字符串超过50,000条，请拆分工作簿')
                    element.clear()
        if member.filename == 'xl/styles.xml' and member.file_size > 1024 * 1024:
            raise ValueError('格式表超过1MB，请另存为简单xlsx后上传')
        if re.fullmatch(r'xl/worksheets/[^/]+\.xml', member.filename):
            if member.file_size > 16 * 1024 * 1024:
                raise ValueError('单工作表解压后超过16MB，请拆分文件')
            row_count = 0
            with package.open(member) as stream:
                for _, element in ET.iterparse(stream, events=('end',)):
                    tag = element.tag.rsplit('}', 1)[-1]
                    if tag == 'row':
                        row_count += 1
                        if row_count > MAX_ROWS + 20 or int(element.get('r', row_count)) > MAX_ROWS + 20:
                            raise ValueError('工作表实际行号或行数超过20,020，请拆分文件')
                    elif tag == 'c':
                        coordinate = element.get('r', '')
                        match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', coordinate)
                        if not match or openpyxl.utils.column_index_from_string(match[1]) > 64 or int(match[2]) > MAX_ROWS + 20:
                            raise ValueError('工作表实际单元格超出20,020行或64列')
                    element.clear()


def read_source(payload):
    try:
        with zipfile.ZipFile(BytesIO(payload)) as package:
            validate_package(package)
        workbook = openpyxl.load_workbook(BytesIO(payload), read_only=True, data_only=False)
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法读取xlsx文件，请确认文件未加密且格式正确') from None
    try:
        if len(workbook.worksheets) > 20:
            raise ValueError('工作表超过20张，请仅保留报价表')
        selected = None
        columns = None
        header_row = None
        for sheet in workbook.worksheets:
            sheet.reset_dimensions()
            for row_number, cells in enumerate(sheet.iter_rows(max_row=20, max_col=64), 1):
                occurrences = Counter(str(cell.value).strip() for cell in cells if cell.value is not None)
                names = {str(cell.value).strip(): cell.column - 1 for cell in cells if cell.value is not None}
                if all(name in names for name in SOURCE_HEADERS):
                    if any(occurrences[name] != 1 for name in SOURCE_HEADERS):
                        raise ValueError('关键表头重复，请确保MCC、MNC、新价、生效时间各只有一列')
                    selected, columns, header_row = sheet, names, row_number
                    break
            if selected is not None:
                break
        if selected is None:
            raise ValueError('未找到MCC、MNC、New Price (EUR/100)、Price Valid from四个表头')
        records = []
        for row_number, cells in enumerate(selected.iter_rows(min_row=header_row + 1), header_row + 1):
            if all(cell.value is None for cell in cells):
                continue
            if len(records) >= MAX_ROWS:
                raise ValueError('报价超过20,000条，请拆分文件')
            record = {'source_row': row_number, 'mcc': '', 'mnc': '', 'new_price': None, 'effective_time': None, 'errors': [], 'duplicate': False}
            if len(cells) <= max(columns[name] for name in SOURCE_HEADERS):
                cells = list(cells) + [openpyxl.cell.read_only.EmptyCell()] * (max(columns.values()) + 1 - len(cells))
            for key, name in [('mcc', 'MCC'), ('mnc', 'MNC')]:
                try:
                    record[key] = code_value(cells[columns[name]], name)
                except ValueError as error:
                    record['errors'].append(str(error))
            try:
                record['new_price'] = decimal_value(cells[columns['New Price (EUR/100)']].value, '新价')
            except ValueError as error:
                record['errors'].append(str(error))
            try:
                record['effective_time'] = date_value(cells[columns['Price Valid from']].value)
            except ValueError as error:
                record['errors'].append(str(error))
            records.append(record)
        if not records:
            raise ValueError('表头下面没有报价数据')
        combinations = Counter((record['mcc'], record['mnc']) for record in records if record['mcc'] and record['mnc'])
        for record in records:
            record['duplicate'] = combinations[(record['mcc'], record['mnc'])] > 1
        return selected.title, list(columns), records
    except (ET.ParseError, IndexError, KeyError, TypeError, OverflowError, zipfile.BadZipFile, OSError):
        raise ValueError('工作簿数据区损坏或引用无效，请修复或另存为xlsx后上传') from None
    finally:
        workbook.close()


def analyze(payload, filename):
    sheet, columns, records = read_source(payload)
    issues = []
    preview = []
    for record in records:
        reasons = list(record['errors'])
        if record['effective_time'] is None and not any('生效时间' in error for error in record['errors']):
            reasons.append('缺少生效时间')
        if record['duplicate']:
            reasons.append('MCC/MNC重复组合，需确认是否保留')
        if reasons:
            issues.append({key: record[key] for key in ['source_row', 'mcc', 'mnc']} | {'reason': '；'.join(reasons)})
        if len(preview) < 12:
            preview.append({key: record[key] for key in ['source_row', 'mcc', 'mnc']} | {
                'new_price': str(record['new_price']) if record['new_price'] is not None else '',
                'effective_time': record['effective_time'].isoformat(sep=' ', timespec='minutes') if record['effective_time'] else '',
                'status': '；'.join(reasons) or '字段可转换',
            })
    return {'filename': filename, 'sheet': sheet, 'source_unit': 'EUR/100', 'columns': columns, 'preview': preview, 'issues': issues, 'stats': {
        'total_rows': len(records), 'missing_dates': sum(record['effective_time'] is None and not any('生效时间' in error for error in record['errors']) for record in records),
        'valid_rows': sum(not record['errors'] for record in records),
        'invalid_rows': sum(bool(record['errors']) for record in records),
        'duplicate_rows': sum(record['duplicate'] for record in records),
    }}


def convert(payload, options):
    if options.get('confirmed') != 'true':
        raise ValueError('请先确认转换规则；本工具仅输出测试文件')
    target_unit = options.get('target_unit')
    if target_unit not in ('per100', 'per1'):
        raise ValueError('请选择目标费率单位')
    missing_policy = options.get('missing_date_policy')
    if missing_policy not in ('skip', 'fill', 'keep'):
        raise ValueError('请选择空生效时间处理方式')
    duplicate_policy = options.get('duplicate_policy')
    if duplicate_policy not in ('skip', 'keep'):
        raise ValueError('请选择重复编码处理方式')
    upstream = decimal_value(options.get('upstream_rate'), '上行费率')
    fallback = date_value(options.get('fallback_date')) if missing_policy == 'fill' else None
    if missing_policy == 'fill' and fallback is None:
        raise ValueError('请填写用于补齐的生效时间')
    _, _, records = read_source(payload)
    workbook = xlwt.Workbook(encoding='utf-8')
    sheet = workbook.add_sheet('Sheet1')
    header_style = xlwt.easyxf('font: bold on; pattern: pattern solid, fore_colour ice_blue;')
    rate_style = xlwt.easyxf(num_format_str='0.##############')
    date_style = xlwt.easyxf(num_format_str='yyyy-mm-dd hh:mm')
    for column, name in enumerate(HEADERS):
        sheet.write(0, column, name, header_style)
        sheet.col(column).width = 6500 if column == 4 else 4200
    exported = skipped = warnings = 0
    for record in records:
        if record['errors'] or (record['duplicate'] and duplicate_policy == 'skip') or (record['effective_time'] is None and missing_policy == 'skip'):
            skipped += 1
            continue
        effective = record['effective_time'] or fallback
        price = record['new_price'] / Decimal(100) if target_unit == 'per1' else record['new_price']
        exported += 1
        sheet.write(exported, 0, record['mcc'])
        sheet.write(exported, 1, record['mnc'])
        sheet.write(exported, 2, float(price), rate_style)
        sheet.write(exported, 3, float(upstream), rate_style)
        if effective is not None:
            sheet.write(exported, 4, effective, date_style)
        if record['duplicate'] or record['effective_time'] is None:
            warnings += 1
    if not exported:
        raise ValueError('按当前规则没有可输出的数据，请查看异常清单并调整规则')
    sheet.set_panes_frozen(True)
    sheet.set_horz_split_pos(1)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue(), {'exported': exported, 'skipped': skipped, 'warnings': warnings, 'duplicates': sum(record['duplicate'] for record in records)}
