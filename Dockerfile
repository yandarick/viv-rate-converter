FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py converter.py quote_reader.py mail_dates.py workflow.py mock_store.py demo_api.py ./
COPY static ./static
RUN mkdir /data && chown 10001:10001 /data
ENV VIV_DATA_DIR=/data
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "1", "--timeout", "45", "--max-requests", "500", "app:app"]
