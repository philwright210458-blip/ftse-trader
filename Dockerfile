FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --uid 10001 --create-home scanner && mkdir -p /app/runtime && chown scanner /app/runtime
USER scanner
VOLUME ["/app/runtime"]
CMD ["python", "ftse_research_cli.py", "service", "--config", "config/research.json"]
