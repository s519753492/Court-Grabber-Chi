# Playwright's image already contains Chromium matching playwright==1.47.0
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=America/Chicago

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
