FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
VOLUME /data/cache
ENV CACHE_DIR=/data/cache PORT=8765
# host networking is required: SSDP discovery + players fetching clips from this box
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
