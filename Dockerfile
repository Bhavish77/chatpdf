FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY seed ./seed
COPY start.sh ./start.sh

RUN chown -R appuser:appuser /app && chmod +x start.sh
USER appuser

ENV PORT=8000
EXPOSE 8000

CMD ["./start.sh"]
