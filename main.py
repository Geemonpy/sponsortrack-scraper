"""
SponsorTrack API
----------------
FastAPI backend serving jobs from Supabase to the static frontend.

Run locally:
    uvicorn main:app --reload --port 8000

Endpoints:
    GET  /jobs            list jobs (filters: badge, category, location, days, search)
    GET  /jobs/{id}       single job
    POST /alerts          save an email address
    GET  /stats           totals for the header counters
    GET  /                health check
"""

import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="SponsorTrack API", version="1.0.0")

# Open CORS so the static index.html can call the API from anywhere (MVP).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AlertIn(BaseModel):
    email: EmailStr


@app.get("/")
def health():
    return {"status": "ok", "service": "SponsorTrack API"}


@app.get("/jobs")
def list_jobs(
    badge: str | None = Query(None, description="verified | likely | hidden"),
    category: str | None = Query(None, description="IT | care"),
    location: str | None = Query(None, description="substring match on location"),
    days: int | None = Query(None, description="posted within last N days (1/7/14/30)"),
    search: str | None = Query(None, description="substring match on title or company"),
    limit: int = Query(200, le=500),
):
    q = supabase.table("jobs").select("*")

    if badge:
        q = q.eq("badge", badge)
    if category:
        q = q.eq("category", category)
    if location:
        q = q.ilike("location", f"%{location}%")
    if days:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        q = q.gte("posted_date", cutoff)
    if search:
        # match title OR company
        q = q.or_(f"title.ilike.%{search}%,company.ilike.%{search}%")

    q = q.order("posted_date", desc=True).limit(limit)
    result = q.execute()
    return {"count": len(result.data), "jobs": result.data}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    result = supabase.table("jobs").select("*").eq("id", job_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    return result.data[0]


@app.post("/alerts")
def create_alert(payload: AlertIn):
    try:
        supabase.table("email_alerts").upsert(
            {"email": payload.email}, on_conflict="email"
        ).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "subscribed", "email": payload.email}


@app.get("/stats")
def stats():
    today = datetime.now(timezone.utc).date().isoformat()

    def count(builder):
        return builder.execute().count or 0

    total = count(supabase.table("jobs").select("id", count="exact"))
    verified = count(supabase.table("jobs").select("id", count="exact").eq("badge", "verified"))
    likely = count(supabase.table("jobs").select("id", count="exact").eq("badge", "likely"))
    today_count = count(
        supabase.table("jobs").select("id", count="exact").gte("posted_date", today)
    )
    return {
        "total": total,
        "verified": verified,
        "likely": likely,
        "today": today_count,
    }
