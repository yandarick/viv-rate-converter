from datetime import datetime, timezone, timedelta
from email import policy
from email.parser import BytesParser
from html import unescape
import re
from zoneinfo import ZoneInfo


TARGET_ZONE = timezone(timedelta(hours=8))
MAX_MAIL_BYTES = 512 * 1024
ZONE_PATTERN = r'(?:Z|(?:UTC|GMT)(?:[+-]\d{1,2}(?::?\d{2})?)?|[+-]\d{2}:?\d{2})'


def parse_time(value, source_zone):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        text = re.sub(r'\s+(?:UTC|GMT)([+-]\d{1,2})(?::?(\d{2}))?$',
                      lambda match: f'{int(match[1]):+03d}:{match[2] or "00"}', text, flags=re.I)
        text = re.sub(r'\s+(?:UTC|GMT)$', '+00:00', text, flags=re.I)
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError('生效日期无法识别，请用年-月-日 时:分填写') from None
    if not 1900 <= result.year <= 2100:
        raise ValueError('生效日期年份超出1900至2100')
    if result.tzinfo is None:
        zone = ZoneInfo(source_zone)
        first = result.replace(tzinfo=zone, fold=0)
        second = result.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError('生效时间处于夏令时切换的歧义或不存在时段，请填写明确UTC时差')
        round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip != result:
            raise ValueError('源时区不存在该生效时间，请核对时间与UTC时差')
        result = first
    return result.astimezone(TARGET_ZONE).replace(tzinfo=None)


def plain_html(value):
    value = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', value, flags=re.S | re.I)
    return unescape(re.sub(r'<[^>]+>', ' ', value))


def timezone_suffix(zone):
    if not zone:
        return ''
    zone = zone.upper()
    if zone in ('Z', 'UTC', 'GMT'):
        return '+00:00'
    if zone.startswith(('UTC', 'GMT')):
        zone = zone[3:]
    sign = zone[0]
    digits = zone[1:].replace(':', '')
    hour, minute = (int(digits), 0) if len(digits) <= 2 else (int(digits[:-2]), int(digits[-2:]))
    if hour > 23 or minute > 59:
        raise ValueError('邮件UTC时差超出范围，请核对日期和时区')
    return f'{sign}{hour:02d}:{minute:02d}'


def complete_zone(match, text):
    tail = text[match.end():match.end()+18]
    if re.match(r'^(?:\d|:\d|[.,]\d|\s*(?:UTC|GMT|[+-]\d))', tail, re.I):
        raise ValueError('邮件日期或时区没有被完整识别，请核对原文并填写明确日期和UTC时差')
    if re.match(r'^\s+(?:PST|PDT|EST|EDT|CST|CDT|CET|CEST|BST|IST)\b', tail, re.I):
        raise ValueError('邮件使用有歧义的时区缩写，请改用明确UTC时差或手动填写日期')


def read_mail(payload, filename):
    if len(payload) > MAX_MAIL_BYTES:
        raise ValueError('邮件内容超过512KB，请仅粘贴与生效日期有关的正文')
    if filename.lower().endswith('.eml'):
        message = BytesParser(policy=policy.default).parsebytes(payload)
        subject = str(message.get('Subject', ''))[:300]
        body = message.get_body(preferencelist=('plain', 'html')) if message.is_multipart() else message
        if body is None or body.get_content_type() not in ('text/plain', 'text/html'):
            raise ValueError('邮件没有可读取的正文，请复制邮件正文')
        try:
            text = body.get_content()
        except (LookupError, UnicodeError):
            raise ValueError('邮件编码无法识别，请复制正文或另存UTF-8文本') from None
        if not isinstance(text, str):
            raise ValueError('邮件正文类型无法识别')
        return subject, plain_html(text) if body.get_content_type() == 'text/html' else text
    if not filename.lower().endswith('.txt'):
        raise ValueError('邮件请选择.eml或UTF-8 .txt，也可以直接粘贴正文；暂不读取邮箱账号')
    try:
        return '', payload.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('文本邮件请使用UTF-8编码') from None


def extract_dates(text):
    pattern = r'(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?(?:[ T]+(\d{1,2}):([0-5]\d)(?::([0-5]\d))?)?(?:\s*(' + ZONE_PATTERN + r'))?'
    candidates = []
    seen = set()
    for match in re.finditer(pattern, text, flags=re.I):
        year, month, day, hour, minute, second, zone = match.groups()
        try:
            stamp = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
        except ValueError:
            continue
        complete_zone(match, text)
        value = stamp.isoformat(timespec='seconds') + timezone_suffix(zone)
        context = text[max(0, match.start() - 50):min(len(text), match.end() + 50)].strip()
        if value not in seen:
            candidates.append({'value': value, 'label': match.group(0).strip(), 'context': context})
            seen.add(value)
        if len(candidates) > 30:
            raise ValueError('邮件日期候选超过30个，请只保留本次报价相关正文')
    month_pattern = r'(?<!\d)(\d{1,2})[ -]([A-Za-z]{3,9})[ ,\-]+(20\d{2})(?:\s+(\d{1,2}):([0-5]\d))?(?:\s*(' + ZONE_PATTERN + r'))?'
    for match in re.finditer(month_pattern, text, flags=re.I):
        day, month, year, hour, minute, zone = match.groups()
        stamp = None
        for month_format in ('%b', '%B'):
            try:
                stamp = datetime.strptime(f'{day} {month} {year}', f'%d {month_format} %Y').replace(hour=int(hour or 0), minute=int(minute or 0))
                break
            except ValueError:
                continue
        if stamp is None:
            continue
        complete_zone(match, text)
        value = stamp.isoformat(timespec='seconds') + timezone_suffix(zone)
        if value not in seen:
            candidates.append({'value': value, 'label': match.group(0), 'context': text[max(0, match.start()-50):match.end()+50].strip()})
            seen.add(value)
    return candidates[:30]


def mail_context(text='', payload=None, filename=''):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_MAIL_BYTES:
        raise ValueError('粘贴的邮件正文超过512KB')
    subject = ''
    if payload is not None:
        subject, body = read_mail(payload, filename)
        text = (text + '\n' + body).strip()
    if len(text.encode('utf-8')) > MAX_MAIL_BYTES:
        raise ValueError('合并后的邮件正文超过512KB')
    return {'subject': subject, 'text_excerpt': text[:1200], 'date_candidates': extract_dates(text)}
