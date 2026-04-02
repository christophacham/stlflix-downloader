FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY downloader.py .

# Mount points:
#   /app/.env            — credentials
#   /app/.jwt_cache.json — cached JWT (optional, persist for speed)
#   /downloads           — output directory

ENV DOWNLOAD_DIR=/downloads

ENTRYPOINT ["python", "downloader.py"]
