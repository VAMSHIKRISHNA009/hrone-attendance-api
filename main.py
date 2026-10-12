"""Employee Attendance & Analytics API."""
import math
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("must be YYYY-MM-DD")


def _parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        raise ValueError("must be YYYY-MM")
    try:
        first = date(int(value[:4]), int(value[5:]), 1)
    except (TypeError, ValueError):
        raise ValueError("must be YYYY-MM")
    next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def _http_date(value: str) -> date:
    try:
        return _parse_date(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _http_month(value: str) -> tuple[date, date]:
    try:
        return _parse_month(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _parse_shift(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        if len(value) != 5 or not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
        return int(h), int(m)
    except (TypeError, ValueError):
        raise ValueError("must be HH:MM")


def _truncate_ms(ms: int) -> datetime:
    if type(ms) is not int or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def _to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp()) * 1000


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _local(punch_in)
    sh, sm = _parse_shift(shift_start)
    eh, em = _parse_shift(shift_end)
    day = local.date()
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        day -= timedelta(days=1)
    return day.isoformat()


def _shift_end(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local_in = _local(punch_in)
    sh, sm = _parse_shift(shift_start)
    eh, em = _parse_shift(shift_end)
    start_day = local_in.date()
    if (eh, em) <= (sh, sm) and (local_in.hour, local_in.minute) < (eh, em):
        start_day -= timedelta(days=1)
    end_day = start_day + (timedelta(days=1) if (eh, em) <= (sh, sm) else timedelta())
    return datetime.combine(end_day, time(eh, em), tzinfo=IST).astimezone(timezone.utc)


def _late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = _local(punch_in)
    h, m = _parse_shift(shift_start)
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = (local - start).total_seconds()
    return math.floor(elapsed_seconds / 60) if elapsed_seconds > 600 else 0


def _work_hours(punch_in: datetime, punch_out: Optional[datetime]) -> Optional[float]:
    if punch_out is None:
        return None
    return float((Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _overtime_minutes(punch_in: datetime, punch_out: Optional[datetime], shift_end: str) -> int:
    if punch_out is None:
        return 0
    end = _shift_end(punch_in, "00:00", shift_end) if False else None
    # _shift_end needs the employee's actual start; caller supplies the computed end below.
    return 0


def _derived(emp: dict, punch_in: Optional[datetime], punch_out: Optional[datetime], status: str) -> dict:
    if status not in PRESENCE or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = _late_minutes(punch_in, emp["shift_start"])
    hours = _work_hours(punch_in, punch_out)
    overtime = 0
    if punch_out is not None:
        end = _shift_end_for_employee(punch_in, emp["shift_start"], emp["shift_end"])
        delta = math.floor((punch_out - end).total_seconds() / 60)
        overtime = delta if delta >= 30 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours is not None and hours < 4.50}


def _shift_end_for_employee(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local_in = _local(punch_in)
    sh, sm = _parse_shift(shift_start)
    eh, em = _parse_shift(shift_end)
    start_day = local_in.date()
    if (eh, em) <= (sh, sm) and (local_in.hour, local_in.minute) < (eh, em):
        start_day -= timedelta(days=1)
    if (eh, em) <= (sh, sm):
        start_day += timedelta(days=1)
    return datetime.combine(start_day, time(eh, em), tzinfo=IST).astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _to_ms(value)
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items() if k != "_id"}
    return value


def _public(doc: dict) -> dict:
    result = _json_value(doc)
    if "date" in result or "status" in result:
        result.setdefault("late_minutes", 0)
        result.setdefault("overtime_minutes", 0)
        result.setdefault("half_day", False)
        result.setdefault("history", [])
    return result


def _ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employees_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employees_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_emp_date")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")
    db.attendance_logs.create_index([("punch_in", DESCENDING)], name="attendance_punch_in")


@app.on_event("startup")
def startup() -> None:
    try:
        client.admin.command("ping")
        _ensure_indexes()
    except PyMongoError:
        # /health reports the unavailable dependency; startup must remain importable.
        pass


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v):
        _parse_date(v); return v

    @model_validator(mode="after")
    def valid_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: _truncate_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v not in PRESENCE: raise ValueError("invalid presence status")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: _truncate_ms(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is not None and v not in ALL_STATUS: raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: _truncate_ms(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _public(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_public(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = _truncate_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    d = _attendance_date(ts, emp["shift_start"], emp["shift_end"])
    derived = _derived(emp, ts, None, body.status)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None, **derived, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for that date")
    return _public(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = _truncate_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= candidate["punch_in"] or ts > candidate["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = _derived(emp, candidate["punch_in"], ts, candidate["status"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return _public(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                   status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from:
        _http_date(date_from); q.setdefault("date", {})["$gte"] = date_from
    if date_to:
        _http_date(date_to); q.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
    if status:
        if status not in ALL_STATUS: raise HTTPException(422, "invalid status")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_public(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


def _find_emp_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    return emp


@app.patch("/attendance/{emp_code}/{date_value}")
def regularize_attendance(emp_code: str, date_value: str, body: RegularizeIn):
    _http_date(date_value)
    emp = _find_emp_or_404(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_value})
    if not old: raise HTTPException(404, "attendance record not found")
    old_status = old["status"]
    new_status = body.status if body.status is not None else old_status
    old_in, old_out = old.get("punch_in"), old.get("punch_out")
    new_in = _truncate_ms(body.punch_in) if body.punch_in is not None else old_in
    new_out = _truncate_ms(body.punch_out) if body.punch_out is not None else old_out
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "absence status cannot have punch times")
        new_in = new_out = None
    else:
        if new_in is None: raise HTTPException(422, "presence status requires punch_in")
        if _attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date_value: raise HTTPException(422, "punch_in is not on the record attendance date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + timedelta(hours=24)): raise HTTPException(422, "invalid punch_out")
    derived = _derived(emp, new_in, new_out, new_status)
    changes = {}
    for key, before, after in [("status", old_status, new_status), ("punch_in", old_in, new_in), ("punch_out", old_out, new_out)]:
        if before != after: changes[key] = {"from": before, "to": after}
    for key, value in derived.items():
        before = old.get(key, False if key == "half_day" else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        if before != value: changes[key] = {"from": before, "to": value}
    if not changes: raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(old.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {"_id": old["_id"], "history": {"$size": history_len}},
        {"$set": {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None: raise HTTPException(409, "attendance was changed concurrently")
    return _public(updated)


def _month_bounds(month: str) -> tuple[str, str]:
    first, last = _http_month(month)
    return first.isoformat(), last.isoformat()


def _round2(value: Any) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round4(value: Any) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _working_days(joined_on: str, first: date, last: date) -> int:
    start = max(_parse_date(joined_on), first)
    return sum((start + timedelta(days=i)).weekday() < 5 for i in range(max(0, (last - start).days + 1)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = _find_emp_or_404(emp_code)
    first_s, last_s = _month_bounds(month); first, last = _parse_date(first_s), _parse_date(last_s)
    pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}}, {"$project": {"status": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "half_day": {"$ifNull": ["$half_day", False]}, "date": 1}}, {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$ne": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}]
    row = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    working = _working_days(emp["joined_on"], first, last)
    present = _round2(row.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present, "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": None if working == 0 else _round2(present / working * 100)}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first_s, last_s = _month_bounds(month)
    match = {"joined_on": {"$lte": last_s}}
    if department is not None: match["department"] = department
    pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}}], "as": "logs"}}, {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}, {"$set": {"is_present": {"$in": ["$logs.status", list(PRESENCE)]}, "weekday": {"$cond": [{"ne": ["$logs.date", None]}, {"$not": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1, 7]]}}, False]}}}, {"$group": {"_id": "$department", "headcount": {"$sum": {"$cond": [{"$ne": ["$logs", None]}, 0, 1]}}, "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$weekday"]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}}, "avg_work_hours": {"$avg": {"$cond": [{"$and": ["$is_present", {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}}, {"$sort": {"_id": 1}}, {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}]
    items = list(db.employees.aggregate(pipeline))
    for x in items:
        x["present_days"] = _round2(x.get("present_days", 0)); x["avg_work_hours"] = None if x.get("avg_work_hours") is None else _round2(x["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_s, last_s = _month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}}, {"$unwind": "$emp"}]
    if department is not None: pipeline.append({"$match": {"emp.department": department}})
    pipeline += [{"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}, {"$match": {"rank": {"$lte": limit}}}, {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start, end = _http_date(from_), _http_date(to)
    if end < start: raise HTTPException(422, "to must not precede from")
    if (end - start).days > 91: raise HTTPException(422, "range must not exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    lo, hi = datetime.combine(start, time(), tzinfo=timezone.utc), datetime.combine(end, time(), tzinfo=timezone.utc)
    pipeline = [{"$documents": [{"_id": "start", "day": lo}, {"_id": "end", "day": hi}]}, {"$densify": {"field": "day", "partitionByFields": [], "range": {"step": 1, "unit": "day", "bounds": [lo, hi + timedelta(days=1)]}}}, {"$lookup": {"from": "employees", "let": {"day": "$day"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}]}}}], "as": "head"}}, {"$lookup": {"from": "attendance_logs", "let": {"day": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}, "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$day"]}}}, {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}}, {"$unwind": "$e"}, {"$match": {"$expr": {"$eq": ["$e.department", department]}}}], "as": "logs"}}, {"$set": {"headcount": {"$size": "$head"}, "present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE)]}}}, "as": "l", "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}}}}, "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}}}, {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}, "is_working_day": {"$not": {"$in": [{"$dayOfWeek": "$day"}, [1, 7]]}}}}, {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}}, {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}}, {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": "$moving_avg_raw"}}, {"$sort": {"date": 1}}]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    for x in items:
        if x["attendance_rate"] is not None: x["attendance_rate"] = _round4(x["attendance_rate"])
        if x["moving_avg_7d"] is not None: x["moving_avg_7d"] = _round4(x["moving_avg_7d"])
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}: raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from: _http_date(date_from); q.setdefault("date", {})["$gte"] = date_from
        if date_to: _http_date(date_to); q.setdefault("date", {})["$lte"] = date_to
        if status: q["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _json_value(explain)}
    if month is None: raise HTTPException(422, "month is required")
    _http_month(month)
    explain_collection = "attendance_logs"
    if endpoint == "employee_monthly":
        if emp_code is None: raise HTTPException(422, "emp_code is required")
        first_s, last_s = _month_bounds(month)
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}}], "cursor": {}}, verbosity="executionStats")
    elif endpoint == "department_summary":
        explain_collection = "employees"
        first_s, last_s = _month_bounds(month); q = {"joined_on": {"$lte": last_s}}
        if department: q["department"] = department
        explain = db.command("explain", {"aggregate": "employees", "pipeline": [{"$match": q}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}], "cursor": {}}, verbosity="executionStats")
    elif endpoint == "late_leaderboard":
        first_s, last_s = _month_bounds(month); q = {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}], "cursor": {}}, verbosity="executionStats")
    else:
        if department is None or from_ is None or to is None: raise HTTPException(422, "department, from and to are required")
        start, end = _http_date(from_), _http_date(to)
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}], "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": explain_collection, "explain": _json_value(explain)}
