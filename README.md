# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
python sample_seed.py
uvicorn app.main:app --port 8000
```

The service creates its indexes at startup. It exposes interactive API documentation at `/docs` and readiness at `/health`.

## Vercel deployment

The repository includes `api/index.py` and `vercel.json` for Vercel's Python runtime. Import the project into Vercel, set these **Production** environment variables, and deploy:

```text
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>/<database>?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

The public docs URL will be `https://<your-vercel-project>.vercel.app/docs`. Do not commit the MongoDB URI.

## Notes

- All API instants are epoch milliseconds; MongoDB stores BSON datetimes.
- Attendance analytics are implemented with MongoDB aggregation pipelines.
- Punch-in, punch-out, and regularization updates are race-safe.
- No `.env`, secrets, Dockerfile, or virtual environment is included.

## Validation performed

The implementation was syntax-checked and exercised with an in-memory MongoDB substitute for employee creation, duplicate protection, timestamp truncation, punch-in/out rules, regularization history, and 422 validation paths. A MongoDB 6.0+ instance is required for the full aggregation suite, including `$densify` and `$setWindowFields`.
