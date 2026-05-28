
FROM python:3.14-slim

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*


RUN pip install --no-cache-dir -r requirements_sandbox.txt    

EXPOSE 8000