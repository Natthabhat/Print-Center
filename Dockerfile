FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DASHBOARD_DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py app.js index.html login.html web_auth.py box_control.py live_status.py camera_monitor.py timelapse.py ./
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
CMD ["python", "app.py"]
