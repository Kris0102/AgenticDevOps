# 1. Start Docker
& .\.venv\Scripts\Activate.ps1
docker-compose -f ./deploy/docker-compose.yml up -d

# 2. Run concurrently (concurrently spawns commands via cmd.exe on Windows,
#    so use "&&" chaining and the .bat activator, not PowerShell syntax)
npx concurrently `
  --names "API,NPM" `
  --prefix-colors "blue,green" `
  ".venv\Scripts\activate.bat && set PYTHONPATH=src && uvicorn src.api.app:app --port 8000 --reload" `
  "cd dashboard && npm run dev"
