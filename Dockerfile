FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
ENV SIGIL_DATA=/data
EXPOSE 8000
CMD ["uvicorn", "sigil.api:app", "--host", "0.0.0.0", "--port", "8000"]
