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

## Notes

- All API instants are epoch milliseconds; MongoDB stores BSON datetimes.
- Attendance analytics are implemented with MongoDB aggregation pipelines.
- Punch-in, punch-out, and regularization updates are race-safe.
- The provided `sample_data/` directory is used by `sample_seed.py` for a local smoke test.
- No `.env`, secrets, Dockerfile, or virtual environment is included.

## Validation performed

The implementation was syntax-checked and exercised against an isolated MongoDB 7 database for employee creation, duplicate protection, timestamp truncation, punch-in/out rules, regularization history, indexes, all four analytics endpoints, and all five explain endpoints. A MongoDB 6.0+ instance is required for the aggregation suite, including `$densify` and `$setWindowFields`.
