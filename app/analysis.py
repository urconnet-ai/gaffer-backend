"""
Gaffer — analysis.py
Claude-powered recommendation engine.
Takes FPL data and generates a structured gameweek briefing.
"""

import os
import httpx
from typing import Optional

CLAUDE_ENDPOINT = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"


async def generate_recommendation(
    team_data: dict,
    gw_info: dict,
    bootstrap: dict,
    fixtures: list,
    picks_data: Optional[dict] = None,
) -> dict:
    """
    Generates a full gameweek recommendation for a given team.
    Returns a structured dict with transfer, captain, chip, and confidence fields.
    """

    context = build_context(team_data, gw_info, bootstrap, fixtures, picks_data)
    prompt  = build_prompt(context)

    raw = await call_claude(prompt)
    return parse_recommendation(raw, team_data, gw_info)


def build_context(team_data, gw_info, bootstrap, fixtures, picks_data) -> str:
    """Builds the full context string passed to Claude."""
    from app.fpl import extract_squad_context

    next_gw    = gw_info.get("next") or gw_info.get("current") or {}
    gw_name    = next_gw.get("name", "Next Gameweek")
    deadline   = next_gw.get("deadline_time", "Unknown")

    # Top form players (for transfer targets)
    players    = bootstrap.get("elements", []) if bootstrap else []
    teams_map  = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])} if bootstrap else {}

    # Get top 20 in-form players by position
    def top_players(pos_id, n=8):
        pos_players = [p for p in players if p.get("element_type") == pos_id and p.get("minutes", 0) > 200]
        pos_players.sort(key=lambda x: float(x.get("form", 0)), reverse=True)
        lines = []
        for p in pos_players[:n]:
            name  = p.get("web_name")
            price = p.get("now_cost", 0) / 10
            form  = p.get("form", "0.0")
            own   = p.get("selected_by_percent", "0")
            team  = teams_map.get(p.get("team"), "?")
            news  = f" [{p['news']}]" if p.get("news") else ""
            lines.append(f"  {name} ({team}) £{price:.1f}m form:{form} owned:{own}%{news}")
        return "\n".join(lines)

    squad_ctx = extract_squad_context(team_data, picks_data or {}, bootstrap or {})

    context = f"""
GAMEWEEK: {gw_name}
DEADLINE: {deadline}

{squad_ctx}

TOP IN-FORM PLAYERS BY POSITION:

Goalkeepers:
{top_players(1, 4)}

Defenders:
{top_players(2)}

Midfielders:
{top_players(3)}

Forwards:
{top_players(4)}
"""
    return context.strip()


def build_prompt(context: str) -> str:
    return f"""You are Gaffer, an expert FPL (Fantasy Premier League) AI co-manager.

Analyse the squad and current FPL data below. Produce a precise gameweek briefing using EXACTLY these headers — no preamble, nothing else:

TRANSFER OUT
Name of the player to transfer out. One player only. If no transfer recommended, write: Hold — no transfer needed.
Reason in one sentence.

TRANSFER IN
Name of the player to bring in. One player only.
Reason in one sentence covering: form, fixture, price.

CAPTAIN
Your captain recommendation. One player only.
Reason in one sentence.

CHIP
Chip advice. If a chip should be played, name it and state exactly why this gameweek.
If no chip: write: Hold chips — [brief reason].

CONFIDENCE
A score from 1.0 to 10.0 representing confidence in these recommendations.
Format: X.X / 10

SUMMARY
Two sentences maximum. The most important thing the manager needs to know this gameweek.

---
DATA:
{context}"""


async def call_claude(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            CLAUDE_ENDPOINT,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"]


def parse_recommendation(raw: str, team_data: dict, gw_info: dict) -> dict:
    """Parses Claude's structured output into a clean dict."""
    sections = {}
    current  = None
    buffer   = []

    KEYS = ["TRANSFER OUT", "TRANSFER IN", "CAPTAIN", "CHIP", "CONFIDENCE", "SUMMARY"]

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
        "transfer_in":  sections.get("TRANSFER IN", ""),
        "captain":      sections.get("CAPTAIN", ""),
        "chip":         sections.get("CHIP", ""),
        "confidence":   sections.get("CONFIDENCE", ""),
        "summary":      sections.get("SUMMARY", ""),
        "raw":          raw,
    }
