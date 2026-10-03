FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tradebot tradebot
COPY results results
COPY config.example.toml .
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "[ -f config.toml ] || cp config.example.toml config.toml; python -m tradebot check && exec python -m tradebot run --i-understand-the-risk"]
