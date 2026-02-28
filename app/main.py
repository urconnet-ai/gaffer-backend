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

from app.fpl import get_team, get_player_data, get_fixtures, get_gameweek_info, get_full_squad_context, build_squad_prompt_context
from app.analysis import generate_recommendation, chat_with_claude
from app.notifications import send_briefing_email
from app.database import save_user, get_user, get_all_active_users

load_dotenv(override=False)  # Never override Railway environment variables

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


@app.get("/debug-env")
def debug_env():
    """Temporary debug endpoint — remove before going public."""
    import os
    return {
        "ANTHROPIC_API_KEY": "SET" if os.getenv("ANTHROPIC_API_KEY") else "MISSING",
        "RESEND_API_KEY":    "SET" if os.getenv("RESEND_API_KEY")    else "MISSING",
        "SUPABASE_URL":      "SET" if os.getenv("SUPABASE_URL")      else "MISSING",
        "SUPABASE_API_KEY":  "SET" if os.getenv("SUPABASE_API_KEY")  else "MISSING",
        "all_env_keys":      [k for k in os.environ.keys() if "ANTH" in k or "RESEND" in k or "SUPA" in k],
    }

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
    try:
        team_data = await get_team(team_id)
        if not team_data:
            raise HTTPException(status_code=404, detail="Team not found")

        print(f"[briefing] fetching data for team {team_id}: {team_data.get('name')}")

        gw_info  = await get_gameweek_info()
        print(f"[briefing] gw_info fetched: {gw_info is not None}")

        players  = await get_player_data()
        print(f"[briefing] players fetched: {players is not None}")

        fixtures = await get_fixtures()
        print(f"[briefing] fixtures fetched: {fixtures is not None}")

        recommendation = await generate_recommendation(team_data, gw_info, players, fixtures)
        print(f"[briefing] recommendation generated successfully")
        return recommendation

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[briefing] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


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


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    team_id: int
    message: str
    history: list = []   # list of {role, content} for multi-turn


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    Conversational interface — ask anything about your squad.
    Maintains conversation history for multi-turn dialogue.
    """
    try:
        team_data = await get_team(req.team_id)
        if not team_data:
            raise HTTPException(status_code=404, detail="Team not found")

        bootstrap = await get_player_data()
        squad_ctx = await get_full_squad_context(req.team_id, bootstrap or {})
        squad_txt = build_squad_prompt_context(team_data, squad_ctx)

        reply = await chat_with_claude(req.message, squad_txt, req.history)
        return {"reply": reply, "team_name": team_data.get("name")}

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[chat] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

