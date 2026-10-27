FROM python:3.12-slim

WORKDIR /srv
RUN pip install --no-cache-dir httpx==0.27.2
COPY loadgen.py .
CMD ["python", "loadgen.py"]
