# Dockerfile.gda-egress-test — Cloud Run Job image that reproduces the
# toolbox's GDA gRPC call from inside Cloud Run, with the same SA and
# same region. Goal: confirm whether the PermissionDenied is environmental
# to Cloud Run egress, or specific to the toolbox's Go SDK call site.

FROM python:3.12-slim

RUN pip install --no-cache-dir \
      google-cloud-geminidataanalytics \
      google-auth

COPY entrypoint.py /entrypoint.py
ENTRYPOINT ["python3", "/entrypoint.py"]
