from collections import Counter
from datetime import datetime
from io import BytesIO
import re
import struct
import zipfile
from types import SimpleNamespace

import openpyxl
import xlrd
from xlrd.compdoc import CompDoc

from converter import MAX_ROWS, code_value, decimal_value, validate_package


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def xls_sheets(payload, selected_sheet=None):
    document = CompDoc(payload)
    stream = document.get_named_stream('Workbook') or document.get_named_stream('Book')
    if not stream or len(stream) > 32 * 1024 * 1024:
        raise ValueError('xls工作簿内容为空或超出32MB')
    position = 0
    shared_string_bytes = 0
    reading_strings = False
    while position + 4 <= len(stream):
        record_type, length = struct.unpack_from('<HH', stream, position)
        if position + length + 4 > len(stream):
            raise ValueError('xls内部记录不完整，请另存为标准Excel')
        if record_type == 0x0006:
            raise ValueError('报价xls包含公式，请在Excel中粘贴为数值后上传')
        if record_type in (0x0203, 0x027E, 0x00FD, 0x0204, 0x0205, 0x0201, 0x00BD, 0x00BE, 0x00D6) and length >= 4:
            row, column = struct.unpack_from('<HH', stream, position + 4)
            last_column = struct.unpack_from('<H', stream, position + 2 + length)[0] if record_type in (0x00BD, 0x00BE) else column
            if row >= MAX_ROWS + 20 or column >= 64 or last_column >= 64:
                raise ValueError('xls实际单元格超出20,020行或64列')
        if record_type == 0x00FC:
            reading_strings = True
            if length >= 8 and struct.unpack_from('<I', stream, position + 8)[0] > 50000:
                raise ValueError('xls共享字符串超过50,000条')
        elif record_type != 0x003C:
            reading_strings = False
        if reading_strings:
            shared_string_bytes += length
            if shared_string_bytes > 4 * 1024 * 1024:
                raise ValueError('xls共享字符串超过4MB')
        if record_type == 0x0200 and length >= 12:
            first_row, last_row, first_col, last_col = struct.unpack_from('<IIHH', stream, position + 4)
            if last_row > MAX_ROWS + 20 or last_col > 64:
                raise ValueError('xls工作表超出20,020行或64列')
        position += length + 4
    book = xlrd.open_workbook(file_contents=payload, on_demand=True, formatting_info=True, ragged_rows=True)
    try:
        if book.nsheets > 20:
            raise ValueError('工作表超过20张，请仅保留报价表')
        if book.nsheets > 1 and not selected_sheet:
            raise ValueError('工作簿有多个工作表，请填写本次报价的工作表名称：' + '、'.join(book.sheet_names()))
        if selected_sheet and selected_sheet not in book.sheet_names():
            raise ValueError('未找到指定工作表：' + selected_sheet)
        for sheet_index in range(book.nsheets):
            sheet = book.sheet_by_index(sheet_index)
            if sheet.nrows > MAX_ROWS + 20 or sheet.ncols > 64:
                raise ValueError('报价表超过20,020行或64列，请拆分文件')
            def rows():
                for index in range(sheet.nrows):
                    cells = []
                    for original in sheet.row(index):
                        value = original.value
                        if original.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, book.datemode)
                        elif original.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                            value = None
                        cell = SimpleNamespace(value=value, data_type='n' if original.ctype == xlrd.XL_CELL_NUMBER else 's', number_format=book.format_map[book.xf_list[original.xf_index].format_key].format_str)
                        if original.ctype == xlrd.XL_CELL_ERROR:
                            cell.data_type = 'e'
                        elif original.ctype == xlrd.XL_CELL_BOOLEAN:
                            cell.value = bool(value)
                        cells.append(cell)
                    yield cells
            yield sheet.name, rows()
    finally:
        book.release_resources()


def workbook_sheets(payload, filename, selected_sheet=None):
    if filename.lower().endswith('.xls'):
        yield from xls_sheets(payload, selected_sheet)
        return
    if not filename.lower().endswith('.xlsx'):
        raise ValueError('报价文件请选择.xls或.xlsx')
    try:
        with zipfile.ZipFile(BytesIO(payload)) as package:
            validate_package(package)
        book = openpyxl.load_workbook(BytesIO(payload), read_only=True, data_only=False)
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法读取xlsx文件，请核对文件格式或加密状态') from None
    try:
        if len(book.worksheets) > 20:
            raise ValueError('工作表超过20张，请仅保留报价表')
        if len(book.worksheets) > 1 and not selected_sheet:
            raise ValueError('工作簿有多个工作表，请填写本次报价的工作表名称：' + '、'.join(book.sheetnames))
        if selected_sheet and selected_sheet not in book.sheetnames:
            raise ValueError('未找到指定工作表：' + selected_sheet)
        for sheet in book.worksheets:
            sheet.reset_dimensions()
            yield sheet.title, sheet.iter_rows()
    finally:
        book.close()


def cell_code(cell, name, business):
    if cell.data_type in ('f', 'e'):
        raise ValueError(f'{name}含公式或Excel错误')
    if business == 'voice' and name == 'MCC':
        value = str(cell.value).strip()
        if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
            value = str(int(cell.value)) if int(cell.value) == cell.value else value
        if not re.fullmatch(r'[0-9]{1,5}', value):
            raise ValueError('CC应为1至5位数字；本语音编码仅供模拟模板演示')
        return value
    return code_value(cell, name)


def combined_codes(cell):
    if cell.data_type in ('f', 'e') or isinstance(cell.value, bool):
        raise ValueError('MCCMNC含公式或Excel错误')
    value = cell.value
    if isinstance(value, (int, float)):
        number = decimal_value(value, 'MCCMNC')
        if number != number.to_integral_value():
            raise ValueError('MCCMNC应为整数编码')
        text = str(int(number))
        pattern = cell.number_format or 'General'
        if re.fullmatch(r'0{5,6}', pattern):
            text = text.zfill(len(pattern))
        elif pattern not in ('General', '@'):
            raise ValueError('MCCMNC数字格式无法可靠解释，请按文本保存')
    else:
        text = str(value or '').strip()
    if not re.fullmatch(r'[0-9]{5,6}', text):
        raise ValueError('MCCMNC须为5或6位数字；国家级或通配报价请配置国家映射')
    return text[:3], text[3:]


def read_quotes(payload, filename, supplier, direction):
    if not payload or len(payload) > 10 * 1024 * 1024:
        raise ValueError('报价文件为空或超过10MB')
    mappings = supplier['headers']
    for side in ('mt', 'mo'):
        label = mappings.get(side, '')
        currencies = set(re.findall(r'\b(?:EUR|USD|GBP|CNY|HKD)\b', label.upper()))
        if currencies and supplier['currency'] not in currencies:
            raise ValueError('报价费率表头币种与供应商配置不一致，请核对；本工具不自动换币种')
        if re.search(r'/\s*100\b', label) and supplier['divisor'] != '100':
            raise ValueError('表头标注每100单位，请将供应商价格除数设为100')
    if mappings.get('mccmnc'):
        code_keys = ['mccmnc']
    elif mappings.get('country'):
        code_keys = ['country']
    else:
        code_keys = ['mcc'] + (['mnc'] if mappings.get('mnc') else [])
    required_keys = code_keys + ['mt' if direction != 'mo' else ('mo' if mappings.get('mo') else 'mt')]
    if direction == 'both' and mappings.get('mo'):
        required_keys.append('mo')
    required_headers = [normalized(mappings.get(key)) for key in required_keys]
    if any(not name for name in required_headers):
        raise ValueError('供应商配置缺少所选方向的必需表头')
    try:
        for sheet_name, rows in workbook_sheets(payload, filename, supplier.get('sheet_name')):
            if supplier.get('sheet_name') and sheet_name != supplier['sheet_name']:
                continue
            selected = None
            records = []
            for source_row, cells in enumerate(rows, 1):
                if source_row > MAX_ROWS + 20 or len(cells) > 64:
                    raise ValueError('报价表超出20,020行或64列')
                if selected is None:
                    if source_row > 20:
                        break
                    names = [normalized(cell.value) for cell in cells]
                    if not all(header in names for header in required_headers):
                        continue
                    counts = Counter(names)
                    used = [normalized(value) for value in mappings.values() if value]
                    if any(counts[name] > 1 for name in used):
                        raise ValueError('映射表头重复，请核对供应商配置')
                    selected = {key: names.index(normalized(value)) for key, value in mappings.items() if value and normalized(value) in names}
                    continue
                if all(cell.value is None or cell.value == '' for cell in cells):
                    continue
                if len(records) >= MAX_ROWS:
                    raise ValueError('报价超过20,000条，请拆分文件')
                empty = openpyxl.cell.read_only.EmptyCell()
                def field(key):
                    position = selected.get(key)
                    return cells[position] if position is not None and position < len(cells) else empty
                item = {'source_row': source_row, 'mcc': '', 'mnc': '', 'raw_mnc': str(field('mnc').value or '')[:128], 'mt': None, 'mo': None, 'date': None, 'errors': [], 'code_issue': ''}
                if mappings.get('mccmnc'):
                    try:
                        item['mcc'], item['mnc'] = combined_codes(field('mccmnc'))
                        item['raw_mnc'] = item['mnc']
                    except ValueError as error:
                        item['errors'].append(str(error))
                elif mappings.get('country'):
                    country = str(field('country').value or '').strip()
                    mapping = supplier.get('country_map', {}).get(country)
                    if mapping:
                        item['mcc'], item['mnc'] = mapping['mcc'], mapping['mnc']
                        item['raw_mnc'] = item['mnc']
                        item['country_mapping'] = country
                    else:
                        item['errors'].append(f'国家代码 {country[:30]} 没有已确认的MCC/MNC映射；不能猜运营商编码')
                else:
                    try:
                        item['mcc'] = cell_code(field('mcc'), 'MCC', supplier['business'])
                    except ValueError as error:
                        item['errors'].append(str(error))
                    if not mappings.get('mnc'):
                        item['code_issue'] = '国家级报价没有MNC，按已配置替代编码规则处理'
                    else:
                        try:
                            item['mnc'] = cell_code(field('mnc'), 'MNC', supplier['business'])
                        except ValueError as error:
                            if field('mnc').data_type in ('f', 'e') or isinstance(field('mnc').value, bool):
                                item['errors'].append(str(error))
                            else:
                                item['code_issue'] = str(error)
                for side in ('mt', 'mo'):
                    target = side
                    if direction == 'mo' and side == 'mo' and not mappings.get('mo'):
                        target = 'mt'
                    needed = direction == 'both' or side == direction
                    if not needed:
                        item[side] = decimal_value('0', '费率')
                        continue
                    cell = field(target)
                    if side == 'mo' and direction != 'mo' and cell.value in (None, ''):
                        item[side] = decimal_value('0', '上行费率')
                        continue
                    try:
                        if cell.data_type in ('f', 'e') or isinstance(cell.value, bool):
                            raise ValueError('费率含公式、布尔值或Excel错误，请改为数值')
                        item[side] = decimal_value(cell.value, '下行费率' if side == 'mt' else '上行费率')
                    except ValueError as error:
                        item['errors'].append(str(error))
                cell = field('date')
                if cell.data_type in ('f', 'e'):
                    item['errors'].append('生效时间含公式或Excel错误')
                elif cell.value not in (None, ''):
                    if not isinstance(cell.value, (datetime, str)):
                        item['errors'].append('生效时间必须是日期单元格或日期文本')
                    else:
                        item['date'] = cell.value
                records.append(item)
            if selected is not None:
                if not records:
                    raise ValueError('报价表没有数据行')
                return sheet_name, selected, records
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法读取报价工作簿，请核对文件是否损坏或另存为标准Excel') from None
    raise ValueError('未找到配置的必需表头：' + '、'.join(mappings[key] for key in required_keys))
