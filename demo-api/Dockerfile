FROM python:3.12-slim
WORKDIR /app
COPY core.py worker.py service.py engine-manifest.json ./
COPY engine/ ./engine/
COPY examples/ ./examples/
ENV PYTHONDONTWRITEBYTECODE=1 DEMO_HOST=0.0.0.0 DEMO_STATE=/state
USER 65532:65532
EXPOSE 8140
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-I", "-B", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8140,timeout=3); c.request('GET','/ready'); r=c.getresponse(); r.read(); c.close(); raise SystemExit(0 if r.status==200 else 1)"]
CMD ["python", "-I", "-B", "-c", "import sys; sys.path.insert(0, '/app'); import service; raise SystemExit(service.main())"]
