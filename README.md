# Employee Attendance & Analytics API

FastAPI + MongoDB attendance service.

## Run locally

1. Use Python 3.11+ and MongoDB 6.0+.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Set `MONGO_URI` and `MONGO_DB` in your shell (or in a local `.env` file that is **not committed**).
4. Start the API:

   ```bash
   uvicorn app.main:app --port 8000
   ```

5. Open `http://localhost:8000/docs`.

Indexes are created at startup. Do not commit `.env`, credentials, database dumps, or a Dockerfile.

## Status

This is a starter implementation and should be validated against the supplied `openapi.yaml` and hidden edge cases before submission. In particular, verify exact response schemas and analytics definitions, add tests for concurrency and overnight shifts, and confirm explain plans on the target MongoDB version.
