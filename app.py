from io import BytesIO
import logging

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from converter import analyze, convert
from workflow import register_workflow
from demo_api import register_demo


app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config.update(MAX_CONTENT_LENGTH=10 * 1024 * 1024, MAX_FORM_PARTS=24, MAX_FORM_MEMORY_SIZE=1024 * 1024)
register_demo(app)
register_workflow(app)


def uploaded_file():
    upload = request.files.get('file')
    if upload is None or not upload.filename or not upload.filename.lower().endswith('.xlsx'):
        raise ValueError('请选择供应商原始报价xlsx文件')
    payload = upload.read()
    if not payload:
        raise ValueError('上传文件为空')
    return payload, upload.filename[:180]


@app.get('/')
def index():
    return app.send_static_file('index.html')


@app.get('/health')
def health():
    return jsonify(status='ok', service='viv-rate-converter', version='2026.11.12', system_mode='mock')


@app.post('/api/analyze')
def analyze_file():
    payload, filename = uploaded_file()
    return jsonify(analyze(payload, filename))


@app.post('/api/convert')
def convert_file():
    payload, _ = uploaded_file()
    output, stats = convert(payload, request.form)
    response = send_file(BytesIO(output), mimetype='application/vnd.ms-excel', as_attachment=True, download_name='Template_sendRate_converted.xls', max_age=0)
    response.headers.update({
        'X-Exported-Rows': str(stats['exported']), 'X-Skipped-Rows': str(stats['skipped']),
        'X-Warning-Rows': str(stats['warnings']), 'X-Duplicate-Rows': str(stats['duplicates']),
    })
    return response


@app.after_request
def security_headers(response):
    response.headers.update({
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(HTTPException)
def http_error(error):
    message = '上传文件超过10MB限制' if error.code == 413 else '请求无法处理'
    return jsonify(error=message), error.code


@app.errorhandler(Exception)
def server_error(error):
    logging.exception('文件处理失败')
    return jsonify(error='文件处理失败，请检查文件格式或稍后重试'), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=18041)
