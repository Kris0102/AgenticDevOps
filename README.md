# Agentic DevOps

This repository contains the Agentic DevOps orchestration system. The steps below show how to set it up and run it on Windows using PowerShell.

## Prerequisites

- Python 3.11 or 3.12 installed and available on your PATH
- Docker Desktop installed and running (optional)
- Git installed

## Setup on Windows

Open terminal in the repository root and run the following commands.

1. Create a virtual environment and install the Python requirements:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

2. Copy the environment template `.env.example` and set the required values:

   Update at least the following values in `.env`:

   - `DATABASE_URL`
   - `POSTGRES_USER`
   - `POSTGRES_PASSWORD`
   - `POSTGRES_DB`
   - `CLUSTER_API_KEY`

   If you will work with private GitHub repositories, also set `GITHUB_TOKEN`.

3. Start the PostgreSQL stack from the `deploy` folder:

   ```powershell
   cd .\deploy
   docker compose up -d
   cd ..
   ```

4. Setup PostgrSQL database correctly

   ```powershell
   alembic upgrade head
   ```

5. Run the main entrypoint from the `src` folder:

   ```powershell
   cd .\src
   python main.py --repo_url https://github.com/Kris0102/Test_env --base-sha fdb59fe600a683a21fd5c167a0658b0337ac2089 --hitl-checkpoints "plan,code,test" --task_description "Add Fahrenheit support to the weather module. In utils.js add a celsiusToFahrenheit(celsius) function (same style/validation as formatTemperature, CommonJS export). In weather.js, make getWeather accept an optional second argument unit ('C' default or 'F'); when 'F', convert the stored Celsius temperature using celsiusToFahrenheit and set the returned unit to 'F'. Keep the existing 'Unknown city' error behavior. Update main.js to print Munich's weather in Fahrenheit. Preserve existing exports and code style."
   ```

## Notes

- The application expects the `.env` file to be in the repository root.
- The `docker compose up -d` command starts the local services required by the run.