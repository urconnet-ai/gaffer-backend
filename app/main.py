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

    # Fetch live points + FT from history in one call
    gw_pts  = team_data.get("summary_event_points") or 0
    live_gw = team_data.get("summary_event_rank") and team_data.get("current_event")
    free_transfers = 1
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as hclient:
            hr = await hclient.get(f"https://fantasy.premierleague.com/api/entry/{req.team_id}/history/")
            if hr.status_code == 200:
                hdata   = hr.json()
                current = hdata.get("current", [])
                if current:
                    latest  = current[-1]
                    # gross points = points scored + any transfer cost deducted
                    # This matches what FPL website shows as your GW total
                    raw_pts  = latest.get("points", 0)
                    xfer_hit = latest.get("event_transfers_cost", 0)
                    gw_pts   = raw_pts + xfer_hit
                    live_gw = latest.get("event")
                    # Calculate free transfers by walking full season history
                    # Rule: each GW you gain 1 FT (max 5). Using transfers costs FTs.
                    # If cost=0, all transfers were free. If cost>0, paid 4pts each extra.
                    ft = 1  # always start season with 1
                    for gw_entry in current[:-1]:  # exclude current GW (not yet locked)
                        made = gw_entry.get("event_transfers", 0)
                        cost = gw_entry.get("event_transfers_cost", 0)
                        if cost == 0:
                            paid = 0
                            free_used = min(made, ft)
                        else:
                            paid = cost // 4
                            free_used = made - paid
                        ft = min(5, max(0, ft - free_used) + 1)
                    # Current GW: if already transferred this GW, subtract
                    if current:
                        cur = current[-1]
                        made_now = cur.get("event_transfers", 0)
                        cost_now = cur.get("event_transfers_cost", 0)
                        if cost_now == 0:
                            ft = max(0, ft - made_now)
                        else:
                            paid_now = cost_now // 4
                            ft = max(0, ft - (made_now - paid_now))
                    free_transfers = max(0, ft)
    except Exception as e:
        print(f"[connect] history fetch failed: {e}")

    return {
        "team_id":        req.team_id,
        "team_name":      team_data.get("name"),
        "total_pts":      team_data.get("summary_overall_points"),
        "gw_pts":         gw_pts,
        "live_gw":        live_gw,
        "free_transfers": free_transfers,
        "overall_rank":   team_data.get("summary_overall_rank"),
        "message":        "Connected. Gaffer is now monitoring your squad.",
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



@app.get("/fixtures")
async def get_fixture_difficulty():
    """
    Returns next 5 GW fixtures for all 20 PL teams with FDR ratings.
    Used by the fixture difficulty table on the frontend.
    """
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            b = await client.get("https://fantasy.premierleague.com/api/bootstrap-static/")
            f = await client.get("https://fantasy.premierleague.com/api/fixtures/")
            b.raise_for_status(); f.raise_for_status()
            bootstrap = b.json()
            fixtures  = f.json()

        teams    = {t["id"]: t for t in bootstrap.get("teams", [])}
        events   = bootstrap.get("events", [])
        
        # Find current and next 5 GWs
        current_gw = next((e["id"] for e in events if e.get("is_current")), None)
        next_gw    = next((e["id"] for e in events if e.get("is_next")), None)
        start_gw   = current_gw or next_gw or 29
        gw_range   = list(range(start_gw, start_gw + 5))

        # Build fixture map: team_id -> {gw: [{opp, home, fdr}]}
        team_fixtures = {tid: {gw: [] for gw in gw_range} for tid in teams}

        for fix in fixtures:
            gw = fix.get("event")
            if gw not in gw_range:
                continue
            if fix.get("finished"):
                continue
            h = fix.get("team_h")
            a = fix.get("team_a")
            h_fdr = fix.get("team_h_difficulty", 3)
            a_fdr = fix.get("team_a_difficulty", 3)

            if h in team_fixtures and gw in team_fixtures[h]:
                team_fixtures[h][gw].append({
                    "opp": teams.get(a, {}).get("short_name", "?"),
                    "home": True,
                    "fdr": h_fdr
                })
            if a in team_fixtures and gw in team_fixtures[a]:
                team_fixtures[a][gw].append({
                    "opp": teams.get(h, {}).get("short_name", "?"),
                    "home": False,
                    "fdr": a_fdr
                })

        # Build response sorted by team name
        result = []
        for tid, team in sorted(teams.items(), key=lambda x: x[1]["name"]):
            gws = []
            for gw in gw_range:
                matches = team_fixtures[tid].get(gw, [])
                gws.append({
                    "gw": gw,
                    "matches": matches,
                    # avg fdr for sorting — blank = 6 (worst)
                    "avg_fdr": round(sum(m["fdr"] for m in matches) / len(matches), 1) if matches else 6
                })
            result.append({
                "id":        tid,
                "name":      team["name"],
                "short":     team["short_name"],
                "gws":       gws,
                "avg_fdr_5": round(sum(g["avg_fdr"] for g in gws) / 5, 1)
            })

        # Sort by easiest fixtures first
        result.sort(key=lambda x: x["avg_fdr_5"])

        return {"teams": result, "gw_range": gw_range}

    except Exception as e:
        import traceback
        print(f"[fixtures] ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))



# ── Transfer Planner ──────────────────────────────────────────────────────────

@app.get("/players/search")
async def search_players(q: str = "", position: str = "", max_price: float = 0):
    """
    Search FPL players by name/team, filter by position and price.
    Returns top 30 matches with form, price, ownership, next fixture.
    """
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            br = await client.get("https://fantasy.premierleague.com/api/bootstrap-static/")
            br.raise_for_status()
            boot = br.json()

        elements   = boot.get("elements", [])
        teams      = {t["id"]: t["short_name"] for t in boot.get("teams", [])}
        pos_map    = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

        q_lower = q.lower().strip()
        results = []

        for p in elements:
            if p.get("status") == "u":  # unavailable/removed
                continue
            pos = pos_map.get(p.get("element_type"), "?")
            price = p.get("now_cost", 0) / 10
            name  = f"{p.get('first_name','')} {p.get('second_name','')}".strip()
            web_name = p.get("web_name", "")
            team = teams.get(p.get("team"), "?")

            # Filters
            if q_lower and q_lower not in name.lower() and q_lower not in web_name.lower() and q_lower not in team.lower():
                continue
            if position and pos != position.upper():
                continue
            if max_price and price > max_price:
                continue

            results.append({
                "id":         p["id"],
                "name":       web_name,
                "full_name":  name,
                "team":       team,
                "position":   pos,
                "price":      price,
                "form":       float(p.get("form") or 0),
                "total_pts":  p.get("total_points", 0),
                "owned_by":   float(p.get("selected_by_percent") or 0),
                "goals":      p.get("goals_scored", 0),
                "assists":    p.get("assists", 0),
                "minutes":    p.get("minutes", 0),
                "status":     p.get("status", "a"),  # a=available, d=doubt, i=injured
                "news":       p.get("news", ""),
                "pts_per_game": float(p.get("points_per_game") or 0),
                "ict_index":  float(p.get("ict_index") or 0),
                "xg_per90":   float(p.get("expected_goals_per_90") or 0),
                "xa_per90":   float(p.get("expected_assists_per_90") or 0),
                "clean_sheets": p.get("clean_sheets", 0),
                "cost_change_event": p.get("cost_change_event", 0),  # price change this GW
            })

        # Sort by form desc, then total pts
        results.sort(key=lambda x: (-x["form"], -x["total_pts"]))
        return {"players": results[:40], "total": len(results)}

    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/transfer-suggestions/{team_id}")
async def get_transfer_suggestions(team_id: int):
    """
    AI-powered transfer suggestions for a specific team.
    Compares squad players to in-form alternatives at same position and similar price.
    """
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            br = await client.get("https://fantasy.premierleague.com/api/bootstrap-static/")
            pr = await client.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{{gw}}/picks/".replace("{gw}", "28"))
            br.raise_for_status()
            boot = br.json()

        elements  = {p["id"]: p for p in boot.get("elements", [])}
        teams     = {t["id"]: t["short_name"] for t in boot.get("teams", [])}
        pos_map   = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

        # Get squad context
        squad_ctx = await get_full_squad_context(team_id)
        if not squad_ctx:
            raise HTTPException(status_code=404, detail="Team not found")

        squad_players = squad_ctx.get("squad_players", [])
        bank = squad_ctx.get("bank", 0)

        suggestions = []
        for sp in squad_players[:11]:  # starting XI only
            pid   = sp.get("id")
            if not pid or pid not in elements:
                continue
            p_el  = elements[pid]
            pos   = p_el.get("element_type")
            price = p_el.get("now_cost", 0)
            form  = float(p_el.get("form") or 0)

            # Find better options at same position, affordable
            budget = price + int(bank * 10)
            candidates = []
            for el in boot.get("elements", []):
                if el["id"] == pid: continue
                if el.get("element_type") != pos: continue
                if el.get("now_cost", 0) > budget: continue
                if el.get("status") == "u": continue
                el_form = float(el.get("form") or 0)
                if el_form <= form + 0.5: continue  # must be meaningfully better
                candidates.append(el)

            candidates.sort(key=lambda x: (-float(x.get("form") or 0), -x.get("total_points", 0)))

            if candidates:
                best = candidates[0]
                suggestions.append({
                    "out": {
                        "id":       pid,
                        "name":     p_el.get("web_name"),
                        "team":     teams.get(p_el.get("team"), "?"),
                        "position": pos_map.get(pos, "?"),
                        "price":    price / 10,
                        "form":     form,
                        "pts":      p_el.get("total_points", 0),
                        "status":   p_el.get("status", "a"),
                        "news":     p_el.get("news", ""),
                    },
                    "in": {
                        "id":       best["id"],
                        "name":     best.get("web_name"),
                        "team":     teams.get(best.get("team"), "?"),
                        "position": pos_map.get(pos, "?"),
                        "price":    best.get("now_cost", 0) / 10,
                        "form":     float(best.get("form") or 0),
                        "pts":      best.get("total_points", 0),
                        "owned_by": float(best.get("selected_by_percent") or 0),
                        "xg_per90": float(best.get("expected_goals_per_90") or 0),
                    },
                    "form_gain":  round(float(best.get("form") or 0) - form, 1),
                    "price_diff": round((best.get("now_cost", 0) - price) / 10, 1),
                })

        suggestions.sort(key=lambda x: -x["form_gain"])
        return {
            "suggestions":  suggestions[:5],
            "bank":         bank,
            "free_transfers": squad_ctx.get("free_transfers", 1),
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ── Feedback ──────────────────────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel

class FeedbackRequest(_BaseModel):
    type:    str
    message: str
    team_id: int | None = None

@app.post("/feedback")
async def submit_feedback(req: FeedbackRequest):
    """Log user feedback to Supabase feedback table."""
    import datetime, os, httpx as _httpx

    entry = {
        "type":       req.type,
        "message":    req.message,
        "team_id":    req.team_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    print(f"[FEEDBACK] {entry}")

    # Write to Supabase if configured
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_API_KEY", "")
    if supabase_url and supabase_key:
        try:
            async with _httpx.AsyncClient(timeout=6.0) as client:
                r = await client.post(
                    f"{supabase_url}/rest/v1/feedback",
                    headers={
                        "apikey":        supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type":  "application/json",
                        "Prefer":        "return=minimal",
                    },
                    json=entry,
                )
                if r.status_code not in (200, 201):
                    print(f"[feedback] Supabase error {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[feedback] Supabase write failed: {e}")
    else:
        # Fallback — append to local file
        try:
            import json
            with open("/tmp/gaffer_feedback.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    return {"ok": True}
