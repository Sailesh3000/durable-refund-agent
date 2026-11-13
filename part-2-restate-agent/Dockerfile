FROM python:3.12-slim

WORKDIR /app

# restate-sdk has no prebuilt Windows wheels, but Linux wheels install in seconds
RUN pip install --no-cache-dir "restate-sdk[serde]" hypercorn openai

COPY main.py tools.py ./

EXPOSE 9080
CMD ["python", "main.py"]
