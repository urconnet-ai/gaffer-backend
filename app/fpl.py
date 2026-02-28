"""
Gaffer — fpl.py
All FPL public API calls. No authentication required.
Data is pulled fresh on each request; caching can be added later.
"""

import httpx
from typing import Optional

FPL_BASE = "https://fantasy.premierleague.com/api"

_client = httpx.AsyncClient(timeout=15.0, headers={
    "User-Agent": "Mozilla/5.0 (compatible; Gaffer/1.0)"
})


async def get_team(team_id: int) -> Optional[dict]:
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team {team_id} error: {e}")
        return None


async def get_team_picks(team_id: int, gameweek: int) -> Optional[dict]:
    """Returns actual GW picks — the 15 players selected, captain, vice-captain, chip played."""
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/event/{gameweek}/picks/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team_picks {team_id} GW{gameweek} error: {e}")
        return None


async def get_team_transfers(team_id: int) -> Optional[list]:
    try:
        r = await _client.get(f"{FPL_BASE}/entry/{team_id}/transfers/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_team_transfers error: {e}")
        return None


async def get_player_data() -> Optional[dict]:
    """
    Full bootstrap-static: all players, teams, positions, prices, form, ownership.
    ~2MB — the main FPL dataset.
    """
    try:
        r = await _client.get(f"{FPL_BASE}/bootstrap-static/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_player_data error: {e}")
        return None


async def get_fixtures() -> Optional[list]:
    try:
        r = await _client.get(f"{FPL_BASE}/fixtures/")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[fpl] get_fixtures error: {e}")
        return None


async def get_gameweek_info() -> Optional[dict]:
    try:
        r = await _client.get(f"{FPL_BASE}/bootstrap-static/")
        r.raise_for_status()
        data  = r.json()
        events = data.get("events", [])
        current_gw = next((e for e in events if e.get("is_current")), None)
        next_gw    = next((e for e in events if e.get("is_next")), None)
        return { "current": current_gw, "next": next_gw }
    except Exception as e:
        print(f"[fpl] get_gameweek_info error: {e}")
        return None


async def get_full_squad_context(team_id: int, bootstrap: dict) -> dict:
    """
    Fetches the actual GW squad picks and builds a rich context dict for Claude.
    Returns both a structured dict and a plain-text summary string.
    """
    gw_info = await get_gameweek_info()
    current = gw_info.get("current") if gw_info else None
    next_gw = gw_info.get("next")    if gw_info else None

    # Use current GW picks if mid-week, otherwise last GW
    target_gw = (current or next_gw or {}).get("id", 29)

    picks_data = await get_team_picks(team_id, target_gw)

    # Fall back to previous GW if current not yet available
    if not picks_data and target_gw > 1:
        picks_data = await get_team_picks(team_id, target_gw - 1)
        print(f"[fpl] fell back to GW{target_gw - 1} picks")

    transfers = await get_team_transfers(team_id)

    players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
    teams_by_id   = {t["id"]: t for t in bootstrap.get("teams", [])}
    pos_map       = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

    squad = []

    if picks_data:
        picks   = picks_data.get("picks", [])
        history = picks_data.get("entry_history", {})
        bank    = history.get("bank", 0) / 10
        free_transfers = history.get("event_transfers", 0)
        chip_played    = picks_data.get("active_chip")

        for pick in picks:
            player = players_by_id.get(pick["element"], {})
            team   = teams_by_id.get(player.get("team"), {})

            # Next 5 fixtures for this player's team
            next_fixtures = _get_next_fixtures(
                player.get("team"), bootstrap.get("events", []),
                bootstrap.get("fixtures_summary", [])
            )

            squad.append({
                "id":           player.get("id"),
                "name":         player.get("web_name", "Unknown"),
                "full_name":    f"{player.get('first_name','')} {player.get('second_name','')}".strip(),
                "position":     pos_map.get(player.get("element_type"), "?"),
                "team":         team.get("short_name", "?"),
                "team_full":    team.get("name", "?"),
                "price":        player.get("now_cost", 0) / 10,
                "form":         float(player.get("form", 0) or 0),
                "total_points": player.get("total_points", 0),
                "minutes":      player.get("minutes", 0),
                "goals":        player.get("goals_scored", 0),
                "assists":      player.get("assists", 0),
                "clean_sheets": player.get("clean_sheets", 0),
                "ownership":    float(player.get("selected_by_percent", 0) or 0),
                "news":         player.get("news", ""),
                "chance_playing": player.get("chance_of_playing_next_round"),
                "is_captain":   pick.get("is_captain", False),
                "is_vice":      pick.get("is_vice_captain", False),
                "position_num": pick.get("position", 0),
                "is_bench":     pick.get("position", 0) > 11,
                "selling_price": pick.get("selling_price", player.get("now_cost", 0)) / 10,
            })
    else:
        bank = 0
        free_transfers = 1
        chip_played = None

    # Recent transfers (last 5)
    recent_transfers = []
    if transfers:
        for t in sorted(transfers, key=lambda x: x.get("event", 0), reverse=True)[:5]:
            p_in  = players_by_id.get(t.get("element_in",  0), {})
            p_out = players_by_id.get(t.get("element_out", 0), {})
            recent_transfers.append({
                "gw":      t.get("event"),
                "in":      p_in.get("web_name", "?"),
                "in_cost": t.get("element_in_cost", 0) / 10,
                "out":     p_out.get("web_name", "?"),
                "out_cost":t.get("element_out_cost", 0) / 10,
            })

    return {
        "squad":            squad,
        "bank":             bank,
        "free_transfers":   free_transfers,
        "chip_played":      chip_played,
        "recent_transfers": recent_transfers,
        "gw_info":          gw_info,
    }


def _get_next_fixtures(team_id: int, events: list, fixtures: list) -> list:
    """Returns abbreviated next-3-fixture difficulty for a team."""
    if not team_id or not fixtures:
        return []
    upcoming = [
        f for f in fixtures
        if not f.get("finished") and (f.get("team_h") == team_id or f.get("team_a") == team_id)
    ]
    upcoming.sort(key=lambda x: x.get("event") or 999)
    result = []
    for f in upcoming[:3]:
        is_home = f.get("team_h") == team_id
        diff    = f.get("team_h_difficulty") if not is_home else f.get("team_a_difficulty")
        result.append({"home": is_home, "difficulty": diff, "gw": f.get("event")})
    return result


def build_squad_prompt_context(team_data: dict, squad_ctx: dict) -> str:
    """
    Builds a rich plain-text squad summary for Claude's prompt.
    Includes actual picks, prices, form, injuries, and recent transfers.
    """
    gw_info     = squad_ctx.get("gw_info", {})
    next_gw     = gw_info.get("next") or gw_info.get("current") or {}
    gw_name     = next_gw.get("name", "Next GW")
    deadline    = next_gw.get("deadline_time", "Unknown")
    bank        = squad_ctx.get("bank", 0)
    ftb         = squad_ctx.get("free_transfers", 1)
    chip        = squad_ctx.get("chip_played")
    squad       = squad_ctx.get("squad", [])
    transfers   = squad_ctx.get("recent_transfers", [])

    lines = [
        f"TEAM: {team_data.get('name')}",
        f"GAMEWEEK: {gw_name} | DEADLINE: {deadline}",
        f"OVERALL RANK: {team_data.get('summary_overall_rank', '?'):,}" if isinstance(team_data.get('summary_overall_rank'), int) else f"OVERALL RANK: {team_data.get('summary_overall_rank', '?')}",
        f"TOTAL POINTS: {team_data.get('summary_overall_points', '?')}",
        f"BANK: £{bank:.1f}m | FREE TRANSFERS: {ftb}",
        f"CHIP ACTIVE: {chip or 'None'}",
        "",
        "── CURRENT SQUAD ──",
    ]

    # Starting XI
    starters = [p for p in squad if not p["is_bench"]]
    bench    = [p for p in squad if p["is_bench"]]

    for p in sorted(starters, key=lambda x: x["position_num"]):
        cap   = " [C]"  if p["is_captain"] else " [VC]" if p["is_vice"] else ""
        news  = f" ⚠ {p['news']}" if p["news"] else ""
        doubt = f" ({p['chance_playing']}% fit)" if p["chance_playing"] is not None and p["chance_playing"] < 100 else ""
        lines.append(
            f"  {p['position']:3} {p['name']:20} {p['team']:4} "
            f"£{p['price']:.1f}m  form:{p['form']:.1f}  "
            f"pts:{p['total_points']}  owned:{p['ownership']:.1f}%"
            f"{cap}{doubt}{news}"
        )

    lines.append("  --- bench ---")
    for p in sorted(bench, key=lambda x: x["position_num"]):
        lines.append(
            f"  {p['position']:3} {p['name']:20} {p['team']:4} "
            f"£{p['price']:.1f}m  form:{p['form']:.1f}  pts:{p['total_points']}"
        )

    # Recent transfers
    if transfers:
        lines.append("")
        lines.append("── RECENT TRANSFERS ──")
        for t in transfers:
            lines.append(f"  GW{t['gw']}: OUT {t['out']} (£{t['out_cost']:.1f}m) → IN {t['in']} (£{t['in_cost']:.1f}m)")

    return "\n".join(lines)
