FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

RUN useradd --create-home --uid 10001 appuser

ENV HOME=/home/appuser
WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN scrapling install && chown -R appuser:appuser /home/appuser

COPY --chown=appuser:appuser . .

USER appuser

CMD ["sh", "-c", "uvicorn web:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
