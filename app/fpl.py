"""
Gaffer — fpl.py
All FPL public API calls. No authentication required.
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
        data   = r.json()
        events = data.get("events", [])
        from datetime import datetime, timezone
        now        = datetime.now(timezone.utc)

        current_gw = next((e for e in events if e.get("is_current")), None)
        next_gw    = next((e for e in events if e.get("is_next")), None)

        # If deadline has already passed for next_gw, find the true upcoming one
        # FPL sometimes has is_next lagging by a few hours after deadline
        if next_gw:
            dl = next_gw.get("deadline_time", "")
            try:
                dl_dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
                if dl_dt < now:
                    # Find first future deadline
                    future = [e for e in events if not e.get("finished", True)]
                    future.sort(key=lambda e: e.get("id", 999))
                    next_gw = future[0] if future else next_gw
            except Exception:
                pass

        # Also ensure current_gw deadline shows correctly
        if current_gw:
            dl = current_gw.get("deadline_time", "")
            try:
                dl_dt = datetime.fromisoformat(dl.replace("Z", "+00:00"))
                if dl_dt < now and next_gw:
                    current_gw = next_gw
            except Exception:
                pass

        return {"current": current_gw, "next": next_gw}
    except Exception as e:
        print(f"[fpl] get_gameweek_info error: {e}")
        return None


async def get_full_squad_context(team_id: int, bootstrap: dict, team_data: dict = None) -> dict:
    """
    Fetches actual GW squad picks and builds a rich context dict.
    Fixes:
    - Uses selling_price (what you get when selling) not now_cost
    - Correctly calculates free transfers remaining (not transfers made)
    - Flags injured/suspended/doubtful players accurately
    """
    gw_info    = await get_gameweek_info()
    current    = gw_info.get("current") if gw_info else None
    next_gw    = gw_info.get("next")    if gw_info else None
    target_gw  = (current or next_gw or {}).get("id", 29)

    picks_data = await get_team_picks(team_id, target_gw)
    if not picks_data and target_gw > 1:
        picks_data = await get_team_picks(team_id, target_gw - 1)
        print(f"[fpl] fell back to GW{target_gw - 1} picks")

    transfers  = await get_team_transfers(team_id)

    players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
    teams_by_id   = {t["id"]: t for t in bootstrap.get("teams", [])}
    pos_map       = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

    squad = []
    bank  = 0
    free_transfers = 1
    chip_played    = None

    if picks_data:
        picks   = picks_data.get("picks", [])
        history = picks_data.get("entry_history", {})
        chip_played = picks_data.get("active_chip")

        # Bank: actual ITB in 0.1m units
        bank = history.get("bank", 0) / 10

        # ── Free transfers calculation ─────────────────────────────────────────
        # FPL API exposes transfers.limit in the /entry/{id}/ response
        # This is the cleanest source — it reflects banked FTs correctly.
        # We pass team_data in (already fetched) and read directly from it.
        # Fallback chain: team_data.transfers.limit → history inference → 1
        if chip_played in ("wildcard", "freehit"):
            free_transfers = 99
        else:
            # Primary: read from team summary (transfers.limit)
            tdata_transfers = team_data.get("transfers", {}) if team_data else {}
            ft_limit = tdata_transfers.get("limit")

            if ft_limit is not None:
                # Subtract any already made this GW
                ft_made_this_gw = history.get("event_transfers", 0)
                ft_cost         = history.get("event_transfers_cost", 0)
                # If no cost, transfers were free — subtract from limit
                free_deducted   = ft_made_this_gw if ft_cost == 0 else 0
                free_transfers  = max(0, ft_limit - free_deducted)
            else:
                # Fallback: infer from history
                ft_made  = history.get("event_transfers", 0)
                ft_cost  = history.get("event_transfers_cost", 0)
                if ft_cost > 0:
                    free_transfers = 0
                elif ft_made == 0:
                    free_transfers = 1  # at least 1, may have 2 banked
                else:
                    free_transfers = max(0, ft_made - ft_made)

        for pick in picks:
            player = players_by_id.get(pick["element"], {})
            team   = teams_by_id.get(player.get("team"), {})

            # ── SELLING PRICE: use pick's selling_price, not now_cost ──────────
            # selling_price = what you actually receive when selling
            # now_cost = current market price (may be higher if price rose)
            selling_price = pick.get("selling_price", player.get("now_cost", 0)) / 10
            now_cost      = player.get("now_cost", 0) / 10

            # ── AVAILABILITY STATUS ──────────────────────────────────────────
            news              = player.get("news", "") or ""
            chance_playing    = player.get("chance_of_playing_next_round")
            chance_this_round = player.get("chance_of_playing_this_round")

            # Classify availability
            if chance_playing == 0 or "suspended" in news.lower():
                availability = "suspended"
            elif chance_playing == 0 or "injured" in news.lower() or (chance_playing is not None and chance_playing == 0):
                availability = "out"
            elif chance_playing is not None and chance_playing <= 25:
                availability = "major_doubt"
            elif chance_playing is not None and chance_playing <= 75:
                availability = "doubt"
            elif news:
                availability = "minor_concern"
            else:
                availability = "available"

            squad.append({
                "id":               player.get("id"),
                "name":             player.get("web_name", "Unknown"),
                "full_name":        f"{player.get('first_name','')} {player.get('second_name','')}".strip(),
                "position":         pos_map.get(player.get("element_type"), "?"),
                "team":             team.get("short_name", "?"),
                "team_full":        team.get("name", "?"),
                "price":            selling_price,      # what you sell for
                "now_cost":         now_cost,            # current market price
                "price_change":     round(now_cost - selling_price, 1),  # gain/loss vs buy price
                "form":             float(player.get("form", 0) or 0),
                "total_points":     player.get("total_points", 0),
                "minutes":          player.get("minutes", 0),
                "goals":            player.get("goals_scored", 0),
                "assists":          player.get("assists", 0),
                "clean_sheets":     player.get("clean_sheets", 0),
                "ownership":        float(player.get("selected_by_percent", 0) or 0),
                "news":             news,
                "chance_playing":   chance_playing,
                "availability":     availability,
                "is_captain":       pick.get("is_captain", False),
                "is_vice":          pick.get("is_vice_captain", False),
                "position_num":     pick.get("position", 0),
                "is_bench":         pick.get("position", 0) > 11,
            })

    # ── Transfers this GW ──────────────────────────────────────────────────────
    gw_transfers = []
    if transfers:
        for t in sorted(transfers, key=lambda x: x.get("event", 0), reverse=True):
            if t.get("event") == target_gw:
                p_in  = players_by_id.get(t.get("element_in",  0), {})
                p_out = players_by_id.get(t.get("element_out", 0), {})
                gw_transfers.append({
                    "gw":      t.get("event"),
                    "in":      p_in.get("web_name", "?"),
                    "in_cost": t.get("element_in_cost", 0) / 10,
                    "out":     p_out.get("web_name", "?"),
                    "out_cost":t.get("element_out_cost", 0) / 10,
                })

    # Recent transfers (last 5 across all GWs)
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
        "squad":             squad,
        "bank":              bank,
        "free_transfers":    free_transfers,
        "chip_played":       chip_played,
        "gw_transfers":      gw_transfers,        # transfers made THIS gameweek
        "recent_transfers":  recent_transfers,    # last 5 across all GWs
        "gw_info":           gw_info,
        "current_gw":        target_gw,
    }


def _get_next_fixtures(team_id: int, events: list, fixtures: list) -> list:
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



def _format_chips_available(chip_availability: dict, in_h2: bool) -> str:
    """Format available chips for Claude — all 4 chips have H1 and H2 versions."""
    if not chip_availability:
        return "CHIPS AVAILABLE: Unknown"
    suffix = "_h2" if in_h2 else "_h1"
    window = "GW20-38" if in_h2 else "GW1-19"
    avail = []
    for chip, label in [("wildcard","Wildcard"), ("3xc","Triple Captain"),
                         ("bboost","Bench Boost"), ("freehit","Free Hit")]:
        if chip_availability.get(f"{chip}{suffix}"):
            avail.append(label)
    if not avail:
        return f"CHIPS AVAILABLE: None remaining in current window ({window})"
    return f"CHIPS AVAILABLE ({window}): {', '.join(avail)}"

def build_squad_prompt_context(team_data: dict, squad_ctx: dict, chip_availability: dict = None) -> str:
    """
    Builds a rich plain-text squad summary for Claude.
    Uses selling prices, flags availability, shows this GW's transfers.
    """
    gw_info          = squad_ctx.get("gw_info", {})
    next_gw          = gw_info.get("next") or gw_info.get("current") or {}
    gw_name          = next_gw.get("name", "Next GW")
    deadline         = next_gw.get("deadline_time", "Unknown")
    bank             = squad_ctx.get("bank", 0)
    ftb              = squad_ctx.get("free_transfers", 1)
    chip             = squad_ctx.get("chip_played")
    squad            = squad_ctx.get("squad", [])
    gw_transfers     = squad_ctx.get("gw_transfers", [])
    recent_transfers = squad_ctx.get("recent_transfers", [])

    avail_map = {
        "suspended":    "🔴 SUSPENDED",
        "out":          "🔴 OUT",
        "major_doubt":  "🟠 MAJOR DOUBT",
        "doubt":        "🟡 DOUBT",
        "minor_concern":"🟡 MINOR CONCERN",
        "available":    "",
    }

    lines = [
        f"TEAM: {team_data.get('name')}",
        f"GAMEWEEK: {gw_name} | DEADLINE: {deadline}",
        f"OVERALL RANK: {team_data.get('summary_overall_rank', '?'):,}" if isinstance(team_data.get('summary_overall_rank'), int) else f"OVERALL RANK: {team_data.get('summary_overall_rank', '?')}",
        f"TOTAL POINTS: {team_data.get('summary_overall_points', '?')}",
        f"BANK: £{bank:.1f}m | FREE TRANSFERS: {'Unlimited (chip active)' if ftb == 99 else ftb}",
        f"CHIP ACTIVE THIS GW: {chip or 'None'}",
        f"{_format_chips_available(chip_availability or {}, squad_ctx.get('in_second_half', False))}",
        "",
        "── STARTING XI ──",
    ]

    starters = [p for p in squad if not p["is_bench"]]
    bench    = [p for p in squad if p["is_bench"]]

    for p in sorted(starters, key=lambda x: x["position_num"]):
        cap     = " [C]"  if p["is_captain"] else " [VC]" if p["is_vice"] else ""
        status  = avail_map.get(p["availability"], "")
        status_str = f"  {status}" if status else ""
        price_note = f" (sell £{p['price']:.1f}m / market £{p['now_cost']:.1f}m)"
        lines.append(
            f"  {p['position']:3} {p['name']:20} {p['team']:4} "
            f"sell:£{p['price']:.1f}m  form:{p['form']:.1f}  "
            f"pts:{p['total_points']}  owned:{p['ownership']:.1f}%"
            f"{cap}{status_str}"
        )
        if p["news"]:
            lines.append(f"       ↳ {p['news']}")

    lines.append("")
    lines.append("── BENCH ──")
    for p in sorted(bench, key=lambda x: x["position_num"]):
        status = avail_map.get(p["availability"], "")
        status_str = f"  {status}" if status else ""
        lines.append(
            f"  {p['position']:3} {p['name']:20} {p['team']:4} "
            f"sell:£{p['price']:.1f}m  form:{p['form']:.1f}{status_str}"
        )
        if p["news"]:
            lines.append(f"       ↳ {p['news']}")

    # This GW's transfers
    if gw_transfers:
        lines.append("")
        lines.append(f"── TRANSFERS THIS GW ({len(gw_transfers)} made) ──")
        for t in gw_transfers:
            lines.append(f"  OUT {t['out']} (£{t['out_cost']:.1f}m) → IN {t['in']} (£{t['in_cost']:.1f}m)")
    else:
        lines.append("")
        lines.append("── TRANSFERS THIS GW: None made yet ──")

    # Recent transfer history
    if recent_transfers:
        lines.append("")
        lines.append("── RECENT TRANSFER HISTORY ──")
        for t in recent_transfers:
            lines.append(f"  GW{t['gw']}: OUT {t['out']} → IN {t['in']}")

    return "\n".join(lines)
