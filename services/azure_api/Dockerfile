FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

LABEL org.opencontainers.image.source="https://github.com/hdduong/aws-idp-custom-platform" \
      org.opencontainers.image.title="loan-document-azure-api"

ENV AWS_EC2_METADATA_DISABLED=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/azure_api/requirements.txt /tmp/requirements.txt
RUN --mount=type=secret,id=enterprise_ca,required=false \
    set -eu; \
    if [ -f /run/secrets/enterprise_ca ]; then \
        export PIP_CERT=/run/secrets/enterprise_ca; \
    fi; \
    pip install --no-cache-dir --requirement /tmp/requirements.txt; \
    groupadd --gid 10001 api; \
    useradd --uid 10001 --gid api --no-create-home --shell /usr/sbin/nologin api

COPY --chown=api:api services /app/services

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "services.azure_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
