#!/bin/bash

# 1. Start Docker
source venv/bin/activate
if docker info > /dev/null 2>&1; then
  echo "Docker is running."
else
  echo "Docker is not running. Please start Docker and try again."
  exit 1
fi
docker-compose -f ./deploy/docker-compose.yml up --build -d

# 2. Run concurrently and explicitly force it to use bash
npx concurrently \
  --shell "bash" \
  --names "API,NPM" \
  --prefix-colors "blue,green" \
  "source venv/bin/activate && export PYTHONPATH=src && uvicorn src.api.app:app --port 8000" \
  "cd dashboard && npm run dev"