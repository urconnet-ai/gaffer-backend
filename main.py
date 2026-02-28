"""
Gaffer Backend — Phase 1
FastAPI server providing:
  - FPL team data proxy (bypasses browser CORS)
  - Gameweek recommendation generation via Claude
  - Email briefing delivery via Resend
  - User registration and preference storage

Run locally:
  pip install -r requirements.txt
  uvicorn app.main:app --reload

Deploy to Railway:
  Push to GitHub → connect Railway → set environment variables
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
import httpx
import os
from dotenv import load_dotenv

from app.fpl import get_team, get_player_data, get_fixtures, get_gameweek_info
from app.analysis import generate_recommendation
from app.notifications import send_briefing_email
from app.database import save_user, get_user, get_all_active_users

load_dotenv()

app = FastAPI(title="Gaffer API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten this to your Gaffer domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    team_id: int
    email: Optional[EmailStr] = None
    mode: str = "assisted"   # advisory | assisted | autopilot


class BriefingRequest(BaseModel):
    team_id: int


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Gaffer API v1"}


@app.get("/team/{team_id}")
async def get_team_data(team_id: int):
    """
    Proxies the FPL public API.
    The browser cannot call FPL directly due to CORS — this endpoint handles it.
    """
    data = await get_team(team_id)
    if not data:
        raise HTTPException(status_code=404, detail="Team not found")
    return data


@app.post("/connect")
async def connect_team(req: ConnectRequest, background_tasks: BackgroundTasks):
    """
    Registers a user, fetches their team, and generates their first analysis.
    If email is provided, queues the initial briefing.
    """
    # Fetch team data
    team_data = await get_team(req.team_id)
    if not team_data:
        raise HTTPException(status_code=404, detail=f"FPL team {req.team_id} not found")

    # Save user to database
    user = {
        "team_id":   req.team_id,
        "team_name": team_data.get("name"),
        "email":     req.email,
        "mode":      req.mode,
    }
    await save_user(user)

    # Generate first recommendation in the background
    if req.email:
        background_tasks.add_task(send_initial_briefing, req.team_id, req.email)

    return {
        "team_id":    req.team_id,
        "team_name":  team_data.get("name"),
        "total_pts":  team_data.get("summary_overall_points"),
        "gw_pts":     team_data.get("summary_event_points"),
        "overall_rank": team_data.get("summary_overall_rank"),
        "message":    "Connected. Gaffer is now monitoring your squad.",
    }


@app.get("/briefing/{team_id}")
async def get_briefing(team_id: int):
    """
    Generates and returns a full gameweek recommendation for the given team.
    This is the core AI endpoint — calls Claude with full squad + fixture context.
    """
    team_data = await get_team(team_id)
    if not team_data:
        raise HTTPException(status_code=404, detail="Team not found")

    gw_info    = await get_gameweek_info()
    players    = await get_player_data()
    fixtures   = await get_fixtures()

    recommendation = await generate_recommendation(team_data, gw_info, players, fixtures)
    return recommendation


@app.post("/send-briefing")
async def send_briefing(req: BriefingRequest, background_tasks: BackgroundTasks):
    """
    Manually triggers a briefing email for a given team.
    In production this is called by the scheduler before each deadline.
    """
    user = await get_user(req.team_id)
    if not user or not user.get("email"):
        raise HTTPException(status_code=404, detail="User not found or no email registered")

    background_tasks.add_task(send_initial_briefing, req.team_id, user["email"])
    return {"status": "queued", "team_id": req.team_id}


# ── Background Tasks ──────────────────────────────────────────────────────────

async def send_initial_briefing(team_id: int, email: str):
    """Called in background after a user connects their team."""
    try:
        team_data = await get_team(team_id)
        gw_info   = await get_gameweek_info()
        players   = await get_player_data()
        fixtures  = await get_fixtures()

        rec = await generate_recommendation(team_data, gw_info, players, fixtures)
        await send_briefing_email(email, team_data.get("name", "Your Team"), rec)
    except Exception as e:
        print(f"[briefing error] team {team_id}: {e}")
