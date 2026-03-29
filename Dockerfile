FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
RUN mkdir -p /tmp/copyfont_outputs
ENV PORT=8080
ENV PRODUCTION=true
CMD exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
