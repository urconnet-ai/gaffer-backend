"""
Gaffer — analysis.py
Claude-powered recommendation engine using real squad picks.
"""

import os
import httpx
from typing import Optional
from app.fpl import get_full_squad_context, build_squad_prompt_context

CLAUDE_ENDPOINT = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"


async def generate_recommendation(
    team_data: dict,
    gw_info: dict,
    bootstrap: dict,
    fixtures: list,
    picks_data: Optional[dict] = None,  # kept for backward compat, now fetched internally
) -> dict:
    team_id    = team_data.get("id")
    squad_ctx  = await get_full_squad_context(team_id, bootstrap, team_data)
    context    = build_squad_prompt_context(team_data, squad_ctx)
    market_ctx = build_market_context(bootstrap, squad_ctx)

    prompt = build_prompt(context, market_ctx)
    print(f"[analysis] prompt built, calling Claude for team {team_id}")

    raw = await call_claude(prompt)
    return parse_recommendation(raw, team_data, squad_ctx.get("gw_info", gw_info))


def build_market_context(bootstrap: dict, squad_ctx: dict) -> str:
    """
    Builds transfer market context.
    Filters out:
    - Players already in the squad
    - Injured players (chance_of_playing_next_round == 0)
    - Suspended players (news contains 'suspended')
    - Players with < 25% chance of playing
    Only shows players who are available or have minor concerns.
    """
    if not bootstrap:
        return ""

    players   = bootstrap.get("elements", [])
    teams_map = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
    pos_map   = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

    owned_ids = {p["id"] for p in squad_ctx.get("squad", [])}
    bank      = squad_ctx.get("bank", 0)

    # Build a set of squad players' selling prices to know what we can afford
    squad_sell_prices = {p["id"]: p["price"] for p in squad_ctx.get("squad", [])}

    lines = ["", "── TRANSFER TARGETS (available, not in squad) ──"]
    lines.append("  Note: all players below are available to play — no injuries or suspensions")

    for pos_id in [2, 3, 4]:
        pos_name = pos_map[pos_id]

        targets = []
        for p in players:
            if p.get("element_type") != pos_id:
                continue
            if p.get("id") in owned_ids:
                continue
            if p.get("minutes", 0) < 200:
                continue

            # ── Strict availability filter ───────────────────────────────────
            news   = (p.get("news") or "").lower()
            chance = p.get("chance_of_playing_next_round")

            # Skip if confirmed out or suspended
            if chance == 0:
                continue
            if "suspended" in news:
                continue
            if "injured" in news and chance is not None and chance <= 25:
                continue
            if chance is not None and chance < 25:
                continue

            form = float(p.get("form", 0) or 0)
            if form < 4.5:
                continue

            targets.append(p)

        targets.sort(key=lambda x: float(x.get("form", 0) or 0), reverse=True)

        lines.append(f"\n  {pos_name} targets (available, form ≥ 4.5):")
        for p in targets[:6]:
            name    = p.get("web_name")
            price   = p.get("now_cost", 0) / 10
            form    = p.get("form", "0.0")
            own     = p.get("selected_by_percent", "0")
            team    = teams_map.get(p.get("team"), "?")
            chance  = p.get("chance_of_playing_next_round")
            minor   = f" ({chance}% fit)" if chance is not None and chance < 100 else ""
            affordable = " [affordable]" if price <= (bank + 5.0) else ""
            lines.append(f"    {name} ({team}) £{price:.1f}m form:{form} owned:{own}%{affordable}{minor}")

    return "\n".join(lines)


def build_prompt(squad_context: str, market_context: str) -> str:
    return f"""You are Gaffer, an expert FPL (Fantasy Premier League) AI co-manager with deep knowledge of player form, fixture difficulty, and squad management strategy.

CRITICAL RULES:
- Player prices shown are SELLING PRICES (what the manager receives) — use these exact figures
- Only recommend transfer targets from the TRANSFER TARGETS section — these are pre-filtered as available and fit
- Never recommend an injured, suspended, or doubtful player as a transfer in
- Free transfers shown are AVAILABLE transfers remaining this gameweek
- If a player has a news item, factor it heavily into your recommendation

Analyse the manager's actual squad below and produce a precise gameweek briefing. Use EXACTLY these headers — no preamble, no extra commentary:

TRANSFER OUT
Name one specific player to transfer out. State their name, team, price.
Give one clear reason: form, injury risk, fixture, or price concern.
If no transfer is needed, write: Hold — squad is well balanced.

TRANSFER IN
Name one specific player to bring in. State their name, team, price.
Give one clear reason covering form, fixture difficulty, and ownership.
Must be affordable given the available bank balance shown.

CAPTAIN
Name your captain pick. One player only.
Give a specific reason: fixture, form, set piece role, or home/away advantage.

CHIP
State whether to play a chip this gameweek and which one.
If no chip: Hold chips — [specific reason tied to upcoming fixtures].

CONFIDENCE
Score from 1.0 to 10.0.
Format exactly: X.X / 10

SUMMARY
Two sentences maximum. The single most important insight for this manager's specific squad.

---
{squad_context}
{market_context}"""


async def call_claude(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    print(f"[claude] key present: {bool(api_key)}")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            CLAUDE_ENDPOINT,
            headers={
                "x-api-key":            api_key,
                "anthropic-version":    "2023-06-01",
                "content-type":         "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": 700,
                "messages":   [{"role": "user", "content": prompt}],
            }
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]


def parse_recommendation(raw: str, team_data: dict, gw_info: dict) -> dict:
    sections = {}
    current  = None
    buffer   = []
    KEYS     = ["TRANSFER OUT", "TRANSFER IN", "CAPTAIN", "CHIP", "CONFIDENCE", "SUMMARY"]

    for line in raw.split("\n"):
        t = line.strip()
        if t in KEYS:
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = t
            buffer  = []
        elif current and t:
            buffer.append(t)

    if current:
        sections[current] = "\n".join(buffer).strip()

    next_gw = gw_info.get("next") or gw_info.get("current") or {}

    return {
        "gameweek":     next_gw.get("name", "Next GW"),
        "deadline":     next_gw.get("deadline_time"),
        "team_name":    team_data.get("name"),
        "transfer_out": sections.get("TRANSFER OUT", ""),
        "transfer_in":  sections.get("TRANSFER IN",  ""),
        "captain":      sections.get("CAPTAIN",      ""),
        "chip":         sections.get("CHIP",         ""),
        "confidence":   sections.get("CONFIDENCE",   ""),
        "summary":      sections.get("SUMMARY",      ""),
        "raw":          raw,
    }


async def chat_with_claude(message: str, squad_context: str, history: list) -> str:
    """
    Multi-turn chat about the user's squad.
    History is a list of {role, content} dicts.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")

    system = f"""You are Gaffer, an expert FPL co-manager. You have full knowledge of the manager's squad below.

CRITICAL RULES FOR ACCURACY:
- Player prices listed as "sell:£X.Xm" are the SELLING PRICES — use these exact figures when discussing what a player costs to sell
- "market £X.Xm" is the current FPL market price — use this when discussing buying cost
- Free transfers shown are what is AVAILABLE to use this gameweek
- "TRANSFERS THIS GW" shows moves already made — reference these accurately
- When suggesting transfer targets, ONLY suggest players who are fully fit and available — never suggest injured or suspended players
- If asked about a player's availability, check their news field carefully

Answer questions about their specific players, transfers, captaincy, chips, and strategy.
Be direct and specific — always reference actual players from their squad with their correct prices.
Keep answers concise — 2-4 sentences unless a detailed breakdown is needed.

{squad_context}"""

    # Build messages array — system context + history + new message
    messages = []
    for h in history[-6:]:  # keep last 6 turns to stay within context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            CLAUDE_ENDPOINT,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": 400,
                "system":     system,
                "messages":   messages,
            }
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]
