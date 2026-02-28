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
    picks_data: Optional[dict] = None,
    chip_availability: dict = None,
) -> dict:
    team_id    = team_data.get("id")
    squad_ctx  = await get_full_squad_context(team_id, bootstrap, team_data)
    context    = build_squad_prompt_context(team_data, squad_ctx, chip_availability)
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
    return f"""You are Gaffer, an expert FPL AI co-manager. Your job is to give precise, squad-specific advice.

READ THIS BEFORE ANSWERING:
- Every recommendation must reference players actually in this squad by name.
- BUDGET: Never suggest a transfer the manager cannot afford. If bank < £0.5m, only recommend like-for-like or cheaper swaps.
- FREE TRANSFERS: If FT shown is 0, any transfer costs 4pts. Only recommend if clearly worth it. Say explicitly: "Requires a 4pt hit."
- CHIPS: Only mention chips shown as available. Do not recommend chips already played. Only recommend playing NOW if this specific GW justifies it over waiting.
- CAPTAIN: Must be a player already in the squad.
- Be specific. Mention actual names, form scores, prices, and fixture opponents.

Reply using EXACTLY these six headers, nothing else before or after:

TRANSFER OUT
[Player name (Team) £Xm] — [specific reason: form score, fixture run, injury risk, or price fall]
OR: Hold — [why the squad is balanced enough not to move]

TRANSFER IN
[Player name (Team) £Xm] — [form + fixture justification]
Confirm it fits within the bank balance shown. If a hit is required, say: "Requires 4pt hit — worth it because X"
OR: N/A if holding

CAPTAIN
[Player name (Team)] — [compare to 2–3 other options in the squad and explain why this player wins]

CHIP
[Chip name] — [specific GW or trigger event that justifies playing it now]
OR: Hold [chip name] — [name the better moment e.g. "DGW confirmed around GW32"]

CONFIDENCE
X.X / 10 — [one-line reason: e.g. "tight budget limits options" or "clear weak link and obvious upgrade"]

SUMMARY
[One sentence: the single most urgent action this GW.]
[One sentence: what to watch before the next deadline.]

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
