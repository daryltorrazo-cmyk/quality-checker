FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8080
WORKDIR /app

COPY requirements.txt requirements-cloud.txt requirements-cloud.lock ./
RUN pip install --no-cache-dir -r requirements-cloud.txt \
    && groupadd --gid 10001 checker \
    && useradd --uid 10001 --gid checker --no-create-home checker

COPY credit_checker/ ./credit_checker/
COPY static/ ./static/
RUN chmod -R a+rX /app/credit_checker /app/static \
    && mkdir -p /app/.local && chown checker:checker /app/.local

USER checker
EXPOSE 8080
CMD ["python", "-m", "credit_checker.server"]
