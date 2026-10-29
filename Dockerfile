# ---- Stage 1: malware gate (build fails here if any signature matches) ----
FROM alpine:3.24 AS malware-scan
WORKDIR /scan
COPY . .
RUN sh malware-scan.sh /scan && touch /scan/.scan-passed

# ---- Stage 2: app ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code
# Forces the scan stage to run (and pass) before anything is installed or copied
COPY --from=malware-scan /scan/.scan-passed /tmp/.scan-passed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# Run DB migrations, then start the API
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
