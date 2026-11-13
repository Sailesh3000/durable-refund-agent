FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "restate-sdk[serde]" hypercorn

# one image serves both experiment services (they pick their file by CMD)
COPY tool_failure.py crash_recovery.py ./

EXPOSE 9081 9082
