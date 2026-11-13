FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "restate-sdk[serde]" hypercorn openai

COPY main.py approval.py ./

EXPOSE 9083
CMD ["python", "main.py"]
