FROM python:3.11-slim

# Dependências de sistema para drivers de banco (Firebird, SQL Server/ODBC)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfbclient2 \
    firebird-dev \
    unixodbc \
    unixodbc-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["--help"]
