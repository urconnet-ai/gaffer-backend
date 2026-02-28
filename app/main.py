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
    Fetches chip availability from history to give Claude accurate context.
    """
    try:
        team_data = await get_team(team_id)
        if not team_data:
            raise HTTPException(status_code=404, detail="Team not found")

        print(f"[briefing] fetching data for team {team_id}: {team_data.get('name')}")

        gw_info  = await get_gameweek_info()
        players  = await get_player_data()
        fixtures = await get_fixtures()

        # Fetch chip availability from history — critical for accurate recommendations
        import httpx as _httpx
        chip_availability = {}
        in_second_half    = False
        try:
            async with _httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                hr = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/history/")
                if hr.status_code == 200:
                    hdata      = hr.json()
                    chips_used = hdata.get("chips", [])
                    current    = hdata.get("current", [])
                    current_event = current[-1]["event"] if current else 19
                    in_second_half = current_event >= 20

                    def _ch(name, half):
                        return any(c["name"] == name and (
                            (half == 1 and c.get("event", 0) <= 19) or
                            (half == 2 and c.get("event", 0) >= 20)
                        ) for c in chips_used)

                    chip_availability = {
                        "wildcard_h1": not _ch("wildcard", 1),
                        "wildcard_h2": not _ch("wildcard", 2),
                        "freehit_h1":  not _ch("freehit",  1),
                        "freehit_h2":  not _ch("freehit",  2),
                        "bboost_h1":   not _ch("bboost",   1),
                        "bboost_h2":   not _ch("bboost",   2),
                        "3xc_h1":      not _ch("3xc",      1),
                        "3xc_h2":      not _ch("3xc",      2),
                        "in_second_half": in_second_half,
                        "current_event": current_event,
                    }
                    print(f"[briefing] chips: {chip_availability}")
        except Exception as ce:
            print(f"[briefing] chip fetch failed: {ce}")

        recommendation = await generate_recommendation(
            team_data, gw_info, players, fixtures, chip_availability=chip_availability
        )
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
        squad_ctx = await get_full_squad_context(req.team_id, bootstrap or {}, team_data)
        squad_txt = build_squad_prompt_context(team_data, squad_ctx)

        reply = await chat_with_claude(req.message, squad_txt, req.history)
        return {"reply": reply, "team_name": team_data.get("name")}

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[chat] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/squad/{team_id}")
async def get_squad(team_id: int):
    """
    Returns full squad data with form, price, ownership, injury news.
    Used by the frontend stats panel.
    """
    try:
        team_data = await get_team(team_id)
        if not team_data:
            raise HTTPException(status_code=404, detail="Team not found")

        bootstrap = await get_player_data()
        squad_ctx = await get_full_squad_context(team_id, bootstrap or {}, team_data)
        return squad_ctx

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"[squad] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mode")
async def update_mode(req: dict):
    """Updates the management mode for a connected user."""
    try:
        team_id = req.get("team_id")
        mode    = req.get("mode", "assisted")
        if team_id:
            await save_user({"team_id": team_id, "mode": mode})
        return {"status": "ok", "mode": mode}
    except Exception as e:
        print(f"[mode] ERROR: {e}")
        return {"status": "error"}



@app.get("/season")
async def get_season():
    """Returns the current FPL season label pulled live from the FPL API."""
    try:
        bootstrap = await get_player_data()
        if not bootstrap:
            return {"season": "2025/26"}

        # FPL events contain the season — derive from first event's deadline year
        events = bootstrap.get("events", [])
        if events:
            # First event deadline tells us the season start year
            first_deadline = events[0].get("deadline_time", "2025-08-01")
            start_year     = int(first_deadline[:4])
            season_label   = f"{start_year}/{str(start_year + 1)[-2:]}"
        else:
            season_label = "2025/26"

        # Also return current GW
        current_gw = next((e["name"] for e in events if e.get("is_current")), None)
        next_gw    = next((e["name"] for e in events if e.get("is_next")),    None)

        return {
            "season":     season_label,
            "current_gw": current_gw,
            "next_gw":    next_gw,
        }
    except Exception as e:
        print(f"[season] error: {e}")
        return {"season": "2025/26"}



@app.get("/history/{team_id}")
async def get_history(team_id: int):
    """
    Returns gameweek-by-gameweek points history for the season.
    Used by the season performance chart.
    """
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/history/")
            r.raise_for_status()
            data = r.json()

        current = data.get("current", [])
        chips   = data.get("chips", [])

        # Build per-GW breakdown
        gw_data = []
        for gw in current:
            gw_data.append({
                "event":        gw.get("event"),
                "points":       gw.get("points", 0),
                "total_points": gw.get("total_points", 0),
                "rank":         gw.get("rank"),
                "overall_rank": gw.get("overall_rank"),
                "bank":         gw.get("bank", 0) / 10,
                "value":        gw.get("value", 0) / 10,
                "event_transfers": gw.get("event_transfers", 0),
                "event_transfers_cost": gw.get("event_transfers_cost", 0),
                "points_on_bench": gw.get("points_on_bench", 0),
                "chip":         next((c["name"] for c in chips if c.get("event") == gw.get("event")), None),
            })

        total_pts   = current[-1]["total_points"] if current else 0
        avg_pts     = round(sum(g["points"] for g in gw_data) / len(gw_data), 1) if gw_data else 0
        best_gw     = max(gw_data, key=lambda x: x["points"]) if gw_data else {}
        worst_gw    = min(gw_data, key=lambda x: x["points"]) if gw_data else {}

        # ── Chip availability ──────────────────────────────────────────────────
        # FPL chip rules:
        #   wildcard:  available twice per season — GW1-19 AND GW20-38 (separate)
        #   freehit:   1x total
        #   bboost:    1x total
        #   3xc:       1x total
        # chips[] from history shows USED chips with their event

        # FPL CHIP RULES (2024/25+):
        # ALL chips are available TWICE per season — once per half.
        # Half 1 = GW1-19, Half 2 = GW20-38.
        # A chip used in H1 does NOT remove the H2 version.
        # chips[] shows event number — use that to determine which half was used.

        def chip_used_in_half(name, half):
            """half=1 means GW1-19, half=2 means GW20-38"""
            for c in chips:
                if c["name"] != name:
                    continue
                ev = c.get("event", 0)
                if half == 1 and ev <= 19:
                    return True
                if half == 2 and ev >= 20:
                    return True
            return False

        chip_availability = {
            # Wildcard: separate H1 and H2 versions
            "wildcard_h1": not chip_used_in_half("wildcard", 1),
            "wildcard_h2": not chip_used_in_half("wildcard", 2),
            # All others: also H1 and H2 separate
            "freehit_h1":  not chip_used_in_half("freehit", 1),
            "freehit_h2":  not chip_used_in_half("freehit", 2),
            "bboost_h1":   not chip_used_in_half("bboost",  1),
            "bboost_h2":   not chip_used_in_half("bboost",  2),
            "3xc_h1":      not chip_used_in_half("3xc",     1),
            "3xc_h2":      not chip_used_in_half("3xc",     2),
        }

        # Current GW to determine which half we are in
        current_event = current[-1]["event"] if current else 19
        in_second_half = current_event >= 20

        return {
            "gameweeks":          gw_data,
            "chips_used":         chips,
            "chip_availability":  chip_availability,
            "in_second_half":     in_second_half,
            "current_event":      current_event,
            "summary": {
                "total_points": total_pts,
                "avg_per_gw":   avg_pts,
                "best_gw":      best_gw,
                "worst_gw":     worst_gw,
                "gws_played":   len(gw_data),
            }
        }

    except Exception as e:
        import traceback
        print(f"[history] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/debug-chips/{team_id}")
async def debug_chips(team_id: int):
    """Debug endpoint — shows raw chip data from FPL for a team."""
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/history/")
        r.raise_for_status()
        data = r.json()
    chips       = data.get("chips", [])
    current     = data.get("current", [])
    last_event  = current[-1]["event"] if current else 0
    in_h2       = last_event >= 20
    used_names  = [c["name"] for c in chips]
    def _ch(name, half):
        return any(c["name"] == name and (
            (half == 1 and c.get("event", 0) <= 19) or
            (half == 2 and c.get("event", 0) >= 20)
        ) for c in chips)

    return {
        "raw_chips":      chips,
        "used_names":     used_names,
        "last_event":     last_event,
        "in_second_half": in_h2,
        "chip_availability": {
            "wildcard_h1": not _ch("wildcard", 1),
            "wildcard_h2": not _ch("wildcard", 2),
            "freehit_h1":  not _ch("freehit",  1),
            "freehit_h2":  not _ch("freehit",  2),
            "bboost_h1":   not _ch("bboost",   1),
            "bboost_h2":   not _ch("bboost",   2),
            "3xc_h1":      not _ch("3xc",      1),
            "3xc_h2":      not _ch("3xc",      2),
        }
    }



@app.get("/debug-ft/{team_id}")
async def debug_ft(team_id: int):
    """Debug endpoint — shows exactly what FPL returns for transfers."""
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        # Entry summary
        r1 = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/")
        entry = r1.json()
        
        # Current GW picks
        from app.fpl import get_gameweek_info
        gw_info = await get_gameweek_info()
        current = gw_info.get("current") or gw_info.get("next") or {}
        gw = current.get("id", 29)
        
        r2 = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/")
        picks = r2.json() if r2.status_code == 200 else {}
        
        entry_history = picks.get("entry_history", {})
        
        return {
            "entry_transfers_object": entry.get("transfers"),
            "entry_transfers_limit":  entry.get("transfers", {}).get("limit"),
            "entry_transfers_made":   entry.get("transfers", {}).get("made"),
            "entry_transfers_cost":   entry.get("transfers", {}).get("cost"),
            "picks_event_transfers":      entry_history.get("event_transfers"),
            "picks_event_transfers_cost": entry_history.get("event_transfers_cost"),
            "current_gw": gw,
        }



@app.get("/debug-ft2/{team_id}")
async def debug_ft2(team_id: int):
    """Calculate FT from history the correct way."""
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/history/")
        data = r.json()
    
    current = data.get("current", [])
    
    # Walk through each GW and simulate FT accumulation
    # Rules: start with 1 FT, gain 1 per GW unused, max 5
    ft = 1
    breakdown = []
    for gw in current:
        event          = gw.get("event")
        made           = gw.get("event_transfers", 0)
        cost           = gw.get("event_transfers_cost", 0)
        free_used      = made if cost == 0 else max(0, made - (cost // 4))
        ft_after       = min(5, ft - free_used + 1)
        breakdown.append({
            "gw": event, "made": made, "cost": cost,
            "ft_before": ft, "free_used": free_used, "ft_after": ft_after
        })
        ft = ft_after
    
    return {
        "calculated_ft": ft,
        "last_5_gws": breakdown[-5:] if breakdown else []
    }

