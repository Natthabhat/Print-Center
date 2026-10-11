FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DASHBOARD_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py app.js index.html box_control.py live_status.py camera_monitor.py ./
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
CMD ["python", "app.py"]
