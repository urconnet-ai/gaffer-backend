"""
Gaffer — fpl.py
All FPL public API calls. No authentication required.
Data is pulled fresh on each request; caching can be added later.
"""

import httpx
from typing import Optional

FPL_BASE = "https://fantasy.premierleague.com/api"

# httpx async client — reused across calls
_client = httpx.AsyncClient(timeout=10.0, headers={
    "User-Agent": "Mozilla/5.0 (compatible; Gaffer/1.0)"
})


async def get_team(team_id: int) -> Optional[dict]:
    """Returns a team's summary — name, points, rank, GW score."""
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team {team_id} error: {e}")
        return None


async def get_team_picks(team_id: int, gameweek: int) -> Optional[dict]:
    """Returns the team's picks (squad selection) for a given gameweek."""
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/event/{gameweek}/picks/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team_picks error: {e}")
        return None


async def get_team_transfers(team_id: int) -> Optional[list]:
    """Returns the team's full transfer history."""
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/transfers/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team_transfers error: {e}")
        return None


async def get_player_data() -> Optional[dict]:
    """
    Returns the full bootstrap-static dataset:
    all players, teams, positions, current prices, form, and ownership.
    This is the main FPL data endpoint — ~2MB of JSON.
    """
    try:
        r = await _client.get(f"{FPL_BASE}/bootstrap-static/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_player_data error: {e}")
        return None


async def get_fixtures() -> Optional[list]:
    """Returns all fixtures for the season including difficulty ratings."""
    try:
        r = await _client.get(f"{FPL_BASE}/fixtures/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_fixtures error: {e}")
        return None


async def get_gameweek_info() -> Optional[dict]:
    """
    Returns info about the current and next gameweeks:
    deadline, status, average score, highest score.
    """
    try:
        r = await _client.get(f"{FPL_BASE}/bootstrap-static/")
        r.raise_for_status()
        data = r.json()

        events = data.get("events", [])
        current_gw = next((e for e in events if e.get("is_current")), None)
        next_gw    = next((e for e in events if e.get("is_next")), None)

        return {
            "current": current_gw,
            "next":    next_gw,
        }
    except Exception as e:
        print(f"[fpl] get_gameweek_info error: {e}")
        return None


def extract_squad_context(team_data: dict, picks_data: dict, bootstrap: dict) -> str:
    """
    Builds a plain-text squad summary for Claude's context window.
    Converts player IDs to readable names with prices and form.
    """
    if not picks_data or not bootstrap:
        return f"Team: {team_data.get('name')}\nTotal Points: {team_data.get('summary_overall_points')}"

    players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
    teams_by_id   = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}

    picks   = picks_data.get("picks", [])
    chips   = team_data.get("chips", [])
    budget  = picks_data.get("entry_history", {}).get("bank", 0) / 10

    lines = [
        f"Team: {team_data.get('name')}",
        f"Overall Rank: {team_data.get('summary_overall_rank'):,}",
        f"Total Points: {team_data.get('summary_overall_points')}",
        f"Available Budget: £{budget:.1f}m",
        f"Free Transfers: {picks_data.get('entry_history', {}).get('event_transfers', 0)}",
        "",
        "Current Squad:",
    ]

    for pick in picks:
        player = players_by_id.get(pick["element"], {})
        name   = player.get("web_name", "Unknown")
        price  = player.get("now_cost", 0) / 10
        form   = player.get("form", "0.0")
        team   = teams_by_id.get(player.get("team"), "?")
        pos    = ["", "GK", "DEF", "MID", "FWD"][player.get("element_type", 0)]
        cap    = " (C)" if pick.get("is_captain") else " (VC)" if pick.get("is_vice_captain") else ""
        bench  = " [bench]" if pick.get("position", 0) > 11 else ""

        lines.append(f"  {pos} {name} ({team}) £{price:.1f}m form:{form}{cap}{bench}")

    used_chips   = [c["name"] for c in chips if c.get("status_for_entry") == "played"]
    unused_chips = [c["name"] for c in chips if c.get("status_for_entry") == "available"]
    lines.append(f"\nChips Used: {', '.join(used_chips) if used_chips else 'None'}")
    lines.append(f"Chips Available: {', '.join(unused_chips) if unused_chips else 'None'}")

    return "\n".join(lines)
