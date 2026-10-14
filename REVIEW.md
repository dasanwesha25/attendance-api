# REVIEW.md

Review of the starter `app/main.py`. Every item below is fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start is applied to the punch-in's own (UTC or server-local) calendar day, not to the IST attendance date. Overnight shifts are not handled. | Shift 09:30, punch-in 09:50 IST (04:20 UTC) is measured against 09:30 UTC, so the result is wrong by 5.5 hours. | Shift start is built in IST from the record's `date` (`shift_start_at`). |
| 2 | `compute_late_minutes` | Grace is tested on whole minutes (`minutes > 10`), not on seconds. | 09:40:01 on a 09:30 shift must give 10 but gives 0 (R2). | Compare seconds against 600, then floor to minutes. |
| 3 | `compute_work_hours` | Python `round()` is banker's rounding on binary floats. Instants are not cut to whole seconds. | 18 seconds (0.005 h) must give 0.01 but gives 0.0. | Integer arithmetic: `(2*seconds + 36) // 72` hundredths, which is exact half-up. Instants are truncated to seconds on input. |
| 4 | `compute_overtime` | There is no 30-minute minimum (`max(0, ...)`). Shift end is placed on the record date, so overnight shifts are wrong. Subtracting an aware IST datetime from a naive Mongo datetime raises `TypeError`. | Punch-out 10 minutes after shift end returns 10 instead of 0. Overnight punch-out returns a large negative number clamped to 0. | `shift_end_at` adds a day for overnight shifts, applies the 30-minute rule, and the client returns aware datetimes (`tz_aware=True`). |
| 5 | `half_day` | It is hard-coded to `False` and never derived from the rounded `work_hours` (R5), and the starter has no punch-out to do it. | A 4.4 h day is never flagged. | `derive_fields` sets `half_day` from the rounded hours at punch-out and after every correction. |
| 6 | `MongoClient(...)` | There is no `tz_aware`, so datetimes come back naive and are silently treated as local time. A hard-coded URI default is used, and there is no server-selection timeout. | Epoch values shift by the server's UTC offset. | `tz_aware=True`. `MONGO_URI` and `MONGO_DB` are required from the environment. 5 s timeout. |
| 7 | `/health` | It always returns 200, even if MongoDB is down. | Stop MongoDB: `/health` still says ok. | It pings MongoDB and returns 503 on failure. |
| 8 | Startup | No index is created anywhere. | `explain` shows COLLSCAN. Duplicate keys are possible. | `ensure_indexes()` runs at startup (and from `/health` as a retry). |
| 9 | `POST /employees` | Check-then-insert race, and no unique index. | 10 parallel creates give several 201s with the same `emp_code`. | Unique index on `emp_code`. `DuplicateKeyError` becomes 409. |
| 10 | `POST /employees` | No validation of `emp_code` format, e-mail, shift format, `joined_on` date, or `shift_start == shift_end`. | `{"emp_code": "x", "joined_on": "banana"}` returns 201. | Pydantic constraints from the OpenAPI schema. 422 on failure. |
| 11 | `POST /employees` | `created_at = datetime.now()` is naive local time, and the response returns a datetime, not epoch milliseconds. | Response shows an ISO string instead of an integer. | UTC datetime stored. The response maps to epoch ms. |
| 12 | `GET /employees` | `skip = page * page_size` skips the whole first page. `total` counts the whole collection, not the filtered set. There is no sort and no limits on `page` / `page_size`. | `page=1` returns the second page. `?department=X` reports the wrong `total`. `page_size=-1` returns 500. | `(page-1)*page_size`, filtered `count_documents`, sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError` (500). | Punch in with `emp_code: "NOPE"`. | 404. |
| 14 | `punch_in` | Time handling: `datetime.fromtimestamp(ms/1000)` is server-local and naive. The date is not IST (R1) and overnight shifts are ignored. `punched_at=0` is falsy and means "now". Seconds, floats and strings are accepted. | Punch-in at 23:30 UTC on a UTC server gets the wrong day. Seconds-looking values are accepted. | Strict ms integer in range (422 otherwise). Truncate to seconds. `attendance_date()` applies the IST and overnight rules. |
| 15 | `punch_in` | `status` is an unvalidated string. | `"status": "ABSENT"` or `"banana"` returns 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 16 | `punch_in` | Check-then-insert race, no unique key. | Two simultaneous requests both return 201 and create two records for the day. | Unique index on `(emp_code, date)`. `DuplicateKeyError` becomes 409. |
| 17 | `punch_in` response | The response leaks `_id` as `id` and returns raw datetimes. | The contract says `_id` never appears. | `record_to_api` maps to the contract shape. |
| 18 | `GET /attendance` | Loads every matching document, sorts and slices in Python. `total` is `len(docs)`. | At 100k records every call is slow and memory-hungry. | Query with `sort`, `skip`, `limit` in MongoDB and `count_documents`. Index `(date desc, emp_code asc)`. |
| 19 | `GET /attendance` | The sort has no `emp_code` tie-break. There is no validation of dates, status, `page` or `page_size`, and no 422 for `date_from > date_to`. Legacy documents without `history` / `half_day` are returned raw, with `id` and datetimes. | `date_from=2026-07-09&date_to=2026-07-01` returns 200. | Contract sort, strict validation, and `record_to_api` supplies defaults. |

**Looked at and judged fine**
- Endpoints declared as plain `def`: FastAPI runs them in a thread pool and PyMongo is thread-safe.
- `status_code=201` on the create endpoints is right.
- Popping `_id` after `insert_one` was the right instinct. I now build the response through explicit mappers instead.
- The helpers are not reusable as they were; I rewrote them as pure functions that take the record date and the employee.
