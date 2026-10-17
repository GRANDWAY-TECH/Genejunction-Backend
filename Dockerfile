# One image recipe for every service: docker build --build-arg SERVICE=order .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY libs/gj_common /libs/gj_common
RUN pip install /libs/gj_common

ARG SERVICE
COPY services/${SERVICE}/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY services/${SERVICE}/ ./

RUN useradd --uid 10001 --no-create-home app && chown -R app /srv
USER app
EXPOSE 8000

# In production run `alembic upgrade head` as a one-off task before rolling out; RUN_MIGRATIONS=false then.
CMD ["sh", "-c", "if [ \"${RUN_MIGRATIONS:-true}\" = true ] && [ -f alembic.ini ]; then alembic upgrade head; fi && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS:-1}"]
