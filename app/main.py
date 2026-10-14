"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read, but real environment variables win)

Layout of this file
  1. configuration / database / indexes
  2. time helpers and business rules R1 - R5  (pure functions, no I/O)
  3. request models and response mappers
  4. query "plans": the single place where each endpoint's main query / pipeline is built.
     The endpoint runs the plan and /admin/explain explains the very same plan.
  5. endpoints
"""
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(_required_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[_required_env("MONGO_DB")]

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same definition is a no-op."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        # employees: unique business key (also serves the emp_code sort of GET /employees and
        # the $lookup joins) and the (department, joined_on) pair used by headcount queries.
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
        # attendance_logs: (emp_code, date) is the natural key. Unique => punch-in is race-safe.
        try:
            db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                            unique=True, name="uq_emp_date")
        except OperationFailure:  # duplicates already in the data: still index it, just not unique
            log.exception("could not build the unique (emp_code, date) index; falling back to non-unique")
            db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="uq_emp_date")
        # (date desc, emp_code asc): the default order of GET /attendance, and the date-range scans
        # behind the leaderboard.
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # MongoDB may still be starting; /health retries and reports 503 meanwhile
        log.exception("index creation at startup failed")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NO_PUNCH = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
MAX_SHIFT_SECONDS = 24 * 3600

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def unprocessable(msg: str, loc: tuple = ("query",)):
    """422 with the same body shape FastAPI uses for its own validation errors."""
    raise HTTPException(status_code=422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_ms(ms: int) -> int:
    """R1: instants are cut to whole seconds before any calculation (and stored that way)."""
    return ms - ms % 1000


def now_ms() -> int:
    return dt_to_ms(datetime.now(timezone.utc))


def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date a punch-in belongs to. For an overnight shift, a punch-in earlier
    than shift_end (IST) belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < parse_hhmm(emp["shift_end"]) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def _ist_moment(day: date, minutes: int) -> datetime:
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=IST)


def shift_start_at(date_str: str, emp: dict) -> datetime:
    return _ist_moment(date.fromisoformat(date_str), parse_hhmm(emp["shift_start"]))


def shift_end_at(date_str: str, emp: dict) -> datetime:
    day = date.fromisoformat(date_str)
    if is_overnight(emp):
        day += timedelta(days=1)  # the shift ends on the next calendar day
    return _ist_moment(day, parse_hhmm(emp["shift_end"]))


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    seconds = int((punch_in - shift_start_at(date_str, emp)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes after shift_end, counted only when that is at least 30."""
    seconds = int((punch_out - shift_end_at(date_str, emp)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up, using integer arithmetic only:
    floor(seconds / 36 + 1/2) hundredths of an hour."""
    seconds = int((punch_out - punch_in).total_seconds())
    hundredths = (2 * seconds + 36) // 72
    return float(Decimal(hundredths) / 100)


def derive_fields(emp: dict, date_str: str, status: str, punch_in: Optional[datetime],
                  punch_out: Optional[datetime]) -> dict:
    """All stored derived values of a record (R2 - R5) for the given final state."""
    if status in NO_PUNCH or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {"work_hours": hours, "late_minutes": late,
            "overtime_minutes": compute_overtime(punch_out, date_str, emp),
            "half_day": hours < 4.5}  # R5 uses the rounded value


def valid_date(value: str, loc: str = "date") -> date:
    try:
        if not re.fullmatch(DATE_PATTERN, value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(f"'{value}' is not a valid YYYY-MM-DD date", ("query", loc))


def month_bounds(month: str) -> tuple[date, date]:
    """First and last calendar day of a 'YYYY-MM' month."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = date(year, mon, 1)
        last = (date(year + (mon == 12), mon % 12 + 1, 1)) - timedelta(days=1)
    except (ValueError, OverflowError):
        unprocessable(f"'{month}' is not a valid YYYY-MM month", ("query", "month"))
    return first, last


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Pure calendar arithmetic, no data involved."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def half_up(value: Decimal | float | int, places: int) -> float:
    """Round half-up (not banker's rounding) for numbers computed in Python (R8)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding="ROUND_HALF_UP"))


# --------------------------------------------------------------------------- #
# 3. Request models and response mappers
# --------------------------------------------------------------------------- #
# Strict ints: floats, strings and bools are rejected with 422; the range rejects "seconds".
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422 (e.g. 2026-02-30)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def _api_value(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_to_api(entry: dict) -> dict:
    changes = {k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
               for k, v in (entry.get("changes") or {}).items()}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"),
            "changes": changes}


def record_to_api(doc: dict) -> dict:
    """Stored document -> API shape. No _id; epoch ms; legacy documents get their defaults."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Query plans (shared by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
# MongoDB expression snippets used inside the pipelines -------------------------------------------
def _round_half_up(expr: Any, places: int) -> dict:
    """MongoDB's $round is round-half-to-even, so do floor(x * 10^p + 0.5) / 10^p instead.
    The tiny epsilon absorbs binary floating point noise (e.g. 8.125 stored as 8.12499999...)."""
    factor = 10 ** places
    return {"$cond": [{"$isNumber": expr},
                      {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5 + 1e-9]}}, factor]},
                      None]}


_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d",
                                                          "onError": None, "onNull": None}}},
                       [2, 3, 4, 5, 6]]}  # $dayOfWeek: 1 = Sunday ... 7 = Saturday
_DAY_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # a half day counts 0.5 (R5)
PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _DAY_VALUE, 0]}
PRESENT_WEEKDAY = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _DAY_VALUE, 0]}  # R6 + R7
IS_LATE = {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}  # a missing late_minutes is not > 0
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_STATUS = lambda s: {"$cond": [{"$eq": ["$status", s]}, 1, 0]}  # noqa: E731


def plan_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        lo = valid_date(date_from, "date_from").isoformat() if date_from else None
        hi = valid_date(date_to, "date_to").isoformat() if date_to else None
        if lo and hi and lo > hi:
            unprocessable("date_from must not be after date_to", ("query", "date_from"))
        flt["date"] = {**({"$gte": lo} if lo else {}), **({"$lte": hi} if hi else {})}
    if status:
        flt["status"] = status
    return {"kind": "find", "collection": "attendance_logs", "filter": flt,
            "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
            "skip": (page - 1) * page_size, "limit": page_size}


def plan_employee_monthly(emp_code: str, month: str) -> dict:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": PRESENT_WEEKDAY},
                    "leave_days": {"$sum": IS_STATUS("LEAVE")},
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": LATE_MINUTES},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "pipeline": pipeline,
            "hint": {"emp_code": 1, "date": 1}}


def plan_department_summary(month: str, department: Optional[str]) -> dict:
    """Runs on `employees` (so people without any log still count, R9) and joins their month of
    logs through the (emp_code, date) index; each employee is reduced to one row *inside* the join."""
    first, last = month_bounds(month)
    emp_filter: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        emp_filter["department"] = department
    counted = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}  # present-status records with work_hours
    pipeline = [
        {"$match": emp_filter},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {"_id": None,
                            "present_days": {"$sum": PRESENT_WEEKDAY},
                            "wh_sum": {"$sum": {"$cond": [counted, "$work_hours", 0]}},
                            "wh_n": {"$sum": {"$cond": [counted, 1, 0]}},
                            "late_count": {"$sum": IS_LATE},
                            "total_late_minutes": {"$sum": LATE_MINUTES},
                            "leave_count": {"$sum": IS_STATUS("LEAVE")},
                            "on_duty_count": {"$sum": IS_STATUS("ON_DUTY")}}},
            ],
            "as": "agg"}},
        {"$set": {"a": {"$arrayElemAt": ["$agg", 0]}}},  # missing for employees without logs
        {"$group": {"_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$a.present_days"},
                    "wh_sum": {"$sum": "$a.wh_sum"}, "wh_n": {"$sum": "$a.wh_n"},
                    "late_count": {"$sum": "$a.late_count"},
                    "total_late_minutes": {"$sum": "$a.total_late_minutes"},
                    "leave_count": {"$sum": "$a.leave_count"},
                    "on_duty_count": {"$sum": "$a.on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                      "present_days": 1,
                      # mean over records = total hours / number of records (not a mean of means)
                      "avg_work_hours": {"$cond": [{"$gt": ["$wh_n", 0]},
                                                   _round_half_up({"$divide": ["$wh_sum", "$wh_n"]}, 2), None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    return {"kind": "aggregate", "collection": "employees", "pipeline": pipeline,
            "hint": {"department": 1, "joined_on": 1}}


def plan_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MINUTES},
                    "late_count": {"$sum": IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, not to the row count
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "pipeline": pipeline,
            "hint": {"date": -1, "emp_code": 1}}


def plan_department_trend(department: str, date_from: str, date_to: str) -> dict:
    d_from, d_to = valid_date(date_from, "from"), valid_date(date_to, "to")
    if d_to < d_from:
        unprocessable("'to' must not be before 'from'", ("query", "to"))
    n_days = (d_to - d_from).days + 1
    if n_days > 92:
        unprocessable("the range may not exceed 92 days", ("query", "to"))
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    headcount = {"$size": {"$filter": {"input": {"$literal": sorted(e["joined_on"] for e in emps)},
                                       "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}
    pipeline = [
        {"$match": {"emp_code": {"$in": [e["emp_code"] for e in emps]},
                    "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        # one stats row per day that has records. $facet always emits exactly one document,
        # even when nothing matched, which is what makes the gap filling below safe.
        {"$facet": {"days": [{"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_ANY_DAY},
                                         "late_count": {"$sum": IS_LATE}}}]}},
        # gap filling: build one row per calendar day of the range and attach the stats of that day
        {"$project": {"rows": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"day": {"$add": [start, {"$multiply": ["$$i", 86_400_000]}]}},
                "in": {"$let": {
                    "vars": {"d": {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}}},
                    "in": {"day": "$$day", "date": "$$d",
                           "stats": {"$arrayElemAt": [{"$filter": {"input": "$days", "as": "s",
                                                                   "cond": {"$eq": ["$$s._id", "$$d"]}}}, 0]}}}}}}}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$set": {"is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
                  "headcount": headcount,
                  "present_count": {"$ifNull": ["$stats.present_count", 0]},
                  "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        # average of the (non-null) rates of this row and up to 6 earlier rows of the range
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": _round_half_up("$moving_avg_7d", 4)}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "pipeline": pipeline,
            "hint": {"emp_code": 1, "date": 1}}


def run_plan(plan: dict) -> list:
    coll = db[plan["collection"]]
    if plan["kind"] == "find":
        return list(coll.find(plan["filter"]).sort(plan["sort"]).skip(plan["skip"]).limit(plan["limit"]))
    return list(coll.aggregate(plan["pipeline"], hint=plan.get("hint")))


def explain_plan(plan: dict) -> dict:
    if plan["kind"] == "find":
        inner: dict = {"find": plan["collection"], "filter": plan["filter"], "sort": dict(plan["sort"]),
                       "limit": plan["limit"]}
        if plan["skip"]:
            inner["skip"] = plan["skip"]
    else:
        inner = {"aggregate": plan["collection"], "pipeline": plan["pipeline"], "cursor": {}}
        if plan.get("hint"):
            inner["hint"] = plan["hint"]
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


# --------------------------------------------------------------------------- #
# 5. Endpoints (plain `def`: FastAPI runs them in a thread pool, PyMongo is thread-safe)
# --------------------------------------------------------------------------- #
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]
MonthParam = Annotated[str, Query(pattern=MONTH_PATTERN)]


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()  # no-op once done; recovers if MongoDB came up after the app
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)  # the unique index decides, so concurrent duplicates give one 201
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    flt = {"department": department} if department else {}
    total = db.employees.count_documents(flt)
    docs = db.employees.find(flt).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_to_api(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punched_at = ms_to_dt(truncate_ms(body.punched_at if body.punched_at is not None else now_ms()))
    day = attendance_date(punched_at, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": punched_at, "punch_out": None,
           **derive_fields(emp, day, body.status, punched_at, None), "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punched_at = ms_to_dt(truncate_ms(body.punched_at if body.punched_at is not None else now_ms()))
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
                                      sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    elapsed = (punched_at - rec["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > MAX_SHIFT_SECONDS:
        unprocessable("punched_at must be after the punch-in and within 24 hours of it", ("body", "punched_at"))
    fields = derive_fields(emp, rec["date"], rec["status"], rec["punch_in"], punched_at)
    # Conditional update: only succeeds while the record is still open, so of two simultaneous
    # punch-outs exactly one matches and the other gets None back -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": punched_at, **fields}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_to_api(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
                    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN), status: Optional[Status] = None,
                    page: PageParam = 1, page_size: PageSizeParam = 20):
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(plan["filter"])
    return {"items": [record_to_api(d) for d in run_plan(plan)], "total": total,
            "page": page, "page_size": page_size}


_REGULARIZE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Final state of the record after the correction, and the `changes` for the history entry."""
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    status = body.status or old["status"]
    supplied_in = ms_to_dt(truncate_ms(body.punch_in)) if body.punch_in is not None else None
    supplied_out = ms_to_dt(truncate_ms(body.punch_out)) if body.punch_out is not None else None
    if status in NO_PUNCH:
        if supplied_in or supplied_out:
            unprocessable(f"status {status} clears the punch times; do not send punch_in/punch_out", ("body",))
        new_in = new_out = None
    else:
        new_in = supplied_in or old["punch_in"]
        new_out = supplied_out or old["punch_out"]
        if new_in is None:
            unprocessable("a presence status requires a punch_in", ("body", "punch_in"))
        if attendance_date(new_in, emp) != rec["date"]:
            unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if new_out is not None:
            gap = (new_out - new_in).total_seconds()
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))
    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(emp, rec["date"], status, new_in, new_out)}
    changes = {f: {"from": old[f], "to": new[f]} for f in _REGULARIZE_FIELDS if old[f] != new[f]}
    if not changes:
        unprocessable("the request does not change anything", ("body",))
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Path(pattern=DATE_PATTERN)], body: RegularizeIn):
    valid_date(date, "date")
    emp = get_employee(emp_code)
    for _ in range(5):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(emp, rec, body)
        entry = {"at": datetime.now(timezone.utc), "by": body.regularized_by, "reason": body.reason,
                 "changes": changes}
        # Optimistic concurrency: apply only if the record is exactly as we read it (same number of
        # history entries and same punch times). Every successful PATCH appends one entry, so a
        # concurrent edit makes this match fail and we re-read instead of overwriting its history.
        unchanged = {"_id": rec["_id"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
                     "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}}
        updated = db.attendance_logs.find_one_and_update(
            unchanged, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_to_api(updated)
    raise HTTPException(409, "the record is being modified concurrently; please retry")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)  # R7
    rows = run_plan(plan_employee_monthly(emp_code, month))
    agg = rows[0] if rows else {}
    present = agg.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": half_up(present, 2), "leave_days": agg.get("leave_days", 0),
            "late_count": agg.get("late_count", 0), "total_late_minutes": agg.get("total_late_minutes", 0),
            "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
            "attendance_pct": half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None}


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    return {"month": month, "items": run_plan(plan_department_summary(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    return {"month": month, "items": run_plan(plan_late_leaderboard(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
                     to: str = Query(..., pattern=DATE_PATTERN)):
    return {"department": department, "items": run_plan(plan_department_trend(department, from_, to))}


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                                    "late_leaderboard", "department_trend"],
                  emp_code: Optional[str] = None, month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
                  department: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                  date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
                  date_to: Optional[str] = Query(None, pattern=DATE_PATTERN), status: Optional[Status] = None,
                  from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                  to: Optional[str] = Query(None, pattern=DATE_PATTERN),
                  page: PageParam = 1, page_size: PageSizeParam = 20):
    def need(**params):
        missing = [k for k, v in params.items() if v is None]
        if missing:
            unprocessable(f"missing required parameter(s): {', '.join(missing)}", ("query", missing[0]))

    if endpoint == "attendance_list":
        plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        plan = plan_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        need(month=month)
        plan = plan_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        need(month=month)
        plan = plan_late_leaderboard(month, limit, department)
    else:
        need(department=department, **{"from": from_}, to=to)
        plan = plan_department_trend(department, from_, to)
    return {"endpoint": endpoint, "collection": plan["collection"], "explain": explain_plan(plan)}
