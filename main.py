import os
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.command_cursor import CommandCursor

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)

def epoch_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def dt_from_ms(value):
    if value is None: return None
    if type(value) is not int:
        raise HTTPException(422, "timestamp must be an integer epoch in milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "invalid timestamp")

def parse_date(s):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")

def parse_month(s):
    try:
        d = date.fromisoformat(s + "-01")
        if len(s) != 7: raise ValueError()
        return d
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be YYYY-MM")

def time_parts(s):
    try:
        h, m = map(int, s.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59) or len(s) != 5: raise ValueError()
        return h, m
    except Exception:
        raise HTTPException(422, "shift times must be HH:MM")

def local(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def shift_bounds(emp, attendance_date):
    d = parse_date(attendance_date)
    sh, sm = time_parts(emp["shift_start"]); eh, em = time_parts(emp["shift_end"])
    start = datetime(d.year,d.month,d.day,sh,sm,tzinfo=IST)
    end_day = d + timedelta(days=1) if (eh,em) <= (sh,sm) else d
    end = datetime(end_day.year,end_day.month,end_day.day,eh,em,tzinfo=IST)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def attendance_day(emp, punch):
    lt = local(punch)
    sh, sm = time_parts(emp["shift_start"]); eh, em = time_parts(emp["shift_end"])
    # Early punches before shift_end on an overnight shift belong to previous shift day.
    d = lt.date()
    if (eh, em) <= (sh, sm) and (lt.hour, lt.minute) < (eh, em):
        d -= timedelta(days=1)
    return d.isoformat()

def late_minutes(punch, emp, day):
    start, _ = shift_bounds(emp, day)
    delta = int((punch - start).total_seconds())
    return delta // 60 if delta > 600 else 0

def derived(emp, day, pin, pout, status):
    if status not in PRESENT:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, emp, day) if pin else 0
    if not pout or not pin:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((pout-pin).total_seconds())
    if seconds < 0: raise HTTPException(422, "punch_out must not precede punch_in")
    hours = float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    _, end = shift_bounds(emp, day)
    overtime = max(0, int((pout-end).total_seconds()) // 60)
    if (pout-end).total_seconds() < 1800: overtime = 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
            "half_day": hours < 4.50}

def public_doc(doc):
    out = dict(doc); out.pop("_id", None)
    for k in ("punch_in","punch_out","created_at"):
        if k in out: out[k] = epoch_ms(out[k])
    for h in out.get("history", []):
        if "at" in h: h["at"] = epoch_ms(h["at"])
        for change in h.get("changes", {}).values():
            for side in ("from","to"):
                if isinstance(change.get(side), datetime): change[side] = epoch_ms(change[side])
    return out

def paginated(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")
    return (page-1)*page_size

def decimal2(v):
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def rate4(v):
    return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @classmethod
    def validate_times(cls, v): return v

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class CorrectionIn(BaseModel):
    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status":"ok"}
    except PyMongoError:
        raise HTTPException(503, "database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    parse_date(body.joined_on); time_parts(body.shift_start); time_parts(body.shift_end)
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return public_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    q = {"department":department} if department else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip(paginated(page,page_size)).limit(page_size))
    for x in items:
        if isinstance(x.get("created_at"),datetime): x["created_at"]=epoch_ms(x["created_at"])
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body: PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day=attendance_day(emp,ts)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,
         **derived(emp,day,ts,None,body.status),"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return public_doc(doc)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
                    status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from: parse_date(date_from); q["date"]["$gte"]=date_from
        if date_to: parse_date(date_to); q["date"]["$lte"]=date_to
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    offset=paginated(page,page_size)
    total=db.attendance_logs.count_documents(q)
    docs=list(db.attendance_logs.find(q,{"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip(offset).limit(page_size))
    return {"items":[public_doc(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day=attendance_day(emp,ts)
    # Try the IST date and previous day for overnight shifts.
    candidates=[day,(parse_date(day)-timedelta(days=1)).isoformat()]
    for d in candidates:
        old=db.attendance_logs.find_one({"emp_code":body.emp_code,"date":d})
        if old and old.get("punch_in") and old.get("punch_out") is None:
            vals=derived(emp,d,old["punch_in"],ts,old["status"])
            updated=db.attendance_logs.find_one_and_update(
                {"emp_code":body.emp_code,"date":d,"punch_out":None,"punch_in":{"$ne":None}},
                {"$set":{"punch_out":ts,**vals}},return_document=ReturnDocument.AFTER)
            if updated: return public_doc(updated)
            raise HTTPException(409,"already punched out")
    raise HTTPException(409,"no open punch-in record for this date")

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code:str,date_str:str,body:CorrectionIn):
    parse_date(date_str)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date_str})
    if not old: raise HTTPException(404,"attendance record not found")
    before={k:old.get(k) for k in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")}
    status=body.status if body.status is not None else old.get("status","PRESENT")
    pin=dt_from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    pout=dt_from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    if status not in PRESENT: pin=pout=None
    if status in PRESENT and pin is None: raise HTTPException(422,"presence status requires punch_in")
    vals=derived(emp,date_str,pin,pout,status)
    after={"status":status,"punch_in":pin,"punch_out":pout,**vals}
    changes={}
    for k,v in after.items():
        ov=before.get(k)
        if ov != v: changes[k]={"from":ov,"to":v}
    if not changes: return public_doc(old)
    entry={"at":now_utc(),"by":body.by,"reason":body.reason,"changes":changes}
    # Atomic update with pipeline-independent append, so concurrent corrections preserve all history.
    result=db.attendance_logs.find_one_and_update(
        {"emp_code":emp_code,"date":date_str},
        {"$set":after,"$push":{"history":entry}},
        return_document=ReturnDocument.AFTER)
    return public_doc(result)

def month_bounds(month):
    first=parse_month(month)
    next_month=date(first.year+1,1,1) if first.month==12 else date(first.year,first.month+1,1)
    return first.isoformat(),(next_month-timedelta(days=1)).isoformat(),next_month

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    first,last,next_month=month_bounds(month)
    emp=db.employees.find_one({"emp_code":emp_code},{"_id":0})
    if not emp: raise HTTPException(404,"employee not found")
    logs=list(db.attendance_logs.aggregate([
        {"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}},
        {"$group":{"_id":None,"records":{"$sum":1},
          "present_days":{"$sum":{"$cond":[{"$in":["$status",list(PRESENT)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
          "late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}},
          "work_hours_total":{"$sum":{"$ifNull":["$work_hours",0]}}}}
    ]))
    x=logs[0] if logs else {"records":0,"present_days":0,"late_minutes":0,"overtime_minutes":0,"work_hours_total":0}
    start=max(parse_date(first),parse_date(emp["joined_on"]))
    workdays=sum(1 for i in range((parse_date(last)-start).days+1) if (start+timedelta(days=i)).weekday()<5) if start<=parse_date(last) else 0
    return {"emp_code":emp_code,"month":month,"working_days":workdays,"records":x["records"],"present_days":decimal2(x["present_days"]),
            "attendance_rate":rate4(x["present_days"]/workdays if workdays else 0),
            "late_minutes":x["late_minutes"],"overtime_minutes":x["overtime_minutes"],"work_hours":decimal2(x["work_hours_total"])}

@app.get("/analytics/departments/summary")
def department_summary(month:str):
    first,last,_=month_bounds(month)
    return list(db.employees.aggregate([
      {"$match":{"joined_on":{"$lte":last}}},
      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[
        {"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first]},{"$lte":["$date",last]}]}}},
        {"$group":{"_id":None,"records":{"$sum":1},"present_days":{"$sum":{"$cond":[{"$in":["$status",list(PRESENT)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
          "late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}},"work_hours_total":{"$sum":{"$ifNull":["$work_hours",0]}}}}
      ],"as":"stats"}},
      {"$unwind":{"path":"$stats","preserveNullAndEmptyArrays":True}},
      {"$group":{"_id":"$department","headcount":{"$sum":1},"records":{"$sum":{"$ifNull":["$stats.records",0]}},
       "present_days":{"$sum":{"$ifNull":["$stats.present_days",0]}},"late_minutes":{"$sum":{"$ifNull":["$stats.late_minutes",0]}},
       "overtime_minutes":{"$sum":{"$ifNull":["$stats.overtime_minutes",0]}},"work_hours_total":{"$sum":{"$ifNull":["$stats.work_hours_total",0]}}}},
      {"$project":{"_id":0,"department":"$_id","headcount":1,"records":1,"present_days":{"$round":["$present_days",2]},
       "late_minutes":1,"overtime_minutes":1,"average_work_hours":{"$round":[{"$cond":[{"$gt":["$records",0]},{"$divide":["$work_hours_total","$records"]},0]},2]} }},
      {"$sort":{"department":1}}
    ]))

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=100)):
    first,last,_=month_bounds(month)
    return list(db.attendance_logs.aggregate([
      {"$match":{"date":{"$gte":first,"$lte":last}}},
      {"$group":{"_id":"$emp_code","late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}}}},
      {"$setWindowFields":{"sortBy":{"late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$sort":{"rank":1,"_id":1}},{"$limit":limit},
      {"$project":{"_id":0,"emp_code":"$_id","late_minutes":1,"rank":1}}
    ]))

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,date_from:str,date_to:str):
    start=parse_date(date_from); end=parse_date(date_to)
    if end<start: raise HTTPException(422,"date_to must be on or after date_from")
    days=(end-start).days+1
    if days>366: raise HTTPException(422,"date range cannot exceed 366 days")
    # Generate the calendar and fill gaps in MongoDB; moving average is computed by window function.
    return list(db.attendance_logs.aggregate([
      {"$match":{"date":{"$gte":date_from,"$lte":date_to}}},
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},
      {"$unwind":"$employee"},{"$match":{"employee.department":department}},
      {"$group":{"_id":"$date","records":{"$sum":1},"present_days":{"$sum":{"$cond":[{"$in":["$status",list(PRESENT)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
        "late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}}}},
      {"$sort":{"_id":1}},
      {"$setWindowFields":{"sortBy":{"_id":1},"output":{"moving_average":{"$avg":"$present_days","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":"$_id","records":1,"present_days":{"$round":["$present_days",2]},"late_minutes":1,"moving_average":{"$round":["$moving_average",2]}}}
    ]))

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str, emp_code:Optional[str]=None, department:Optional[str]=None,
            date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,
            month:Optional[str]=None,limit:int=10,page:int=1,page_size:int=20):
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        return db.command({"explain":{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":paginated(page,page_size),"limit":min(page_size,100)},"verbosity":"executionStats"})
    if endpoint=="employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month required")
        first,last,_=month_bounds(month); pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":first,"$lte":last}}}]
        return db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})
    if endpoint=="department_summary":
        if not month: raise HTTPException(422,"month required")
        first,last,_=month_bounds(month)
        pipe=[{"$match":{"joined_on":{"$lte":last}}},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first]},{"$lte":["$date",last]}]}}}],"as":"logs"}}]
        return db.command({"explain":{"aggregate":"employees","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})
    if endpoint=="late_leaderboard":
        if not month: raise HTTPException(422,"month required")
        first,last,_=month_bounds(month); pipe=[{"$match":{"date":{"$gte":first,"$lte":last}}},{"$group":{"_id":"$emp_code","late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}}}}]
        return db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})
    if endpoint=="department_trend":
        if not department or not date_from or not date_to: raise HTTPException(422,"department, date_from and date_to required")
        pipe=[{"$match":{"date":{"$gte":date_from,"$lte":date_to}}}]
        return db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})
    raise HTTPException(404,"unknown explain endpoint")
