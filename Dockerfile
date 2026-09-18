FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    KITE_TOKEN_FILE=/data/.kite_token \
    PAPER_TRADING_FILE=/data/paper_trades.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# The daily Kite token and the paper ledger live here so both services share
# them and they survive container rebuilds.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
