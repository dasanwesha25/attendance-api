# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas URI
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000

A local `.env` with the same two variables also works; real environment variables take priority.
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers.

## Status
All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
`REVIEW.md` (Part A) and `DECISIONS.md` (Part C) are filled in.

## Interpretation notes
- Trend range limit: up to 92 calendar days (both ends included) is accepted; 93 or more gives 422.
- Moving average: computed over the reported (4-decimal) `attendance_rate` values.
- Half-up rounding: MongoDB `$round` rounds half to even, so the pipelines use `floor(x*10^n + 0.5)`.
- Overnight punch-in before shift end belongs to the previous day's shift, so its late minutes are measured from that day's shift start (R1 + R2 applied literally).
- Explain: each endpoint builds its query in one `plan_*` function used by both the endpoint and `/admin/explain`. Aggregations pass an index hint.