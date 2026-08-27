FROM python:3.12-alpine

RUN adduser -D -H -u 10001 agent
COPY push_compose.py /push_compose.py
COPY diagnose_compose_scan.py /diagnose_compose_scan.py
USER agent

CMD ["python3", "/push_compose.py"]
