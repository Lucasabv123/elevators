# syntax=docker/dockerfile:1
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# ---- switch: APP_ENV=prod|local  ----
ARG APP_ENV=prod
# For local/offline, bake SQLite DB from Excel into the image
RUN if [ "$APP_ENV" = "local" ]; then python etl.py; else echo "Skipping ETL in prod"; fi

# Streamlit/port
ENV STREAMLIT_SERVER_HEADLESS=true
EXPOSE 8501

# Respect $PORT if the platform injects it (Render does)
CMD ["sh", "-c", "streamlit run invoice_app.py --server.address=0.0.0.0 --server.port=${PORT:-8501}"]
