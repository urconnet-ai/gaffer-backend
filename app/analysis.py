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
    # Extract key squad constraints to force confidence variance
    bank     = 0.0
    ft       = 1
    for line in squad_context.split("\n"):
        if "BANK:" in line:
            try:
                bank = float(line.split("£")[1].split("m")[0])
            except Exception:
                pass
        if "FREE TRANSFERS:" in line:
            try:
                ft = int(line.split("FREE TRANSFERS:")[1].strip().split()[0].replace("Unlimited", "99"))
            except Exception:
                pass

    # Pre-calculate confidence modifier so Claude matches it
    base_conf = 7.5
    if bank < 0.3:
        base_conf -= 1.2
    elif bank < 1.0:
        base_conf -= 0.5
    if ft == 0:
        base_conf -= 0.8
    elif ft >= 2:
        base_conf += 0.4
    if ft >= 3:
        base_conf += 0.5
    conf_hint = round(min(9.5, max(4.0, base_conf)), 1)

    return f"""You are Gaffer, an expert FPL AI co-manager. Give precise, squad-specific advice only.

HARD RULES:
1. Reference actual players in this squad by name — never generic advice.
2. BUDGET £{bank:.1f}m in bank. Never recommend a transfer the manager cannot afford.
3. FREE TRANSFERS: {ft} available. If 0 FT, state "Requires 4pt hit" and only recommend if clearly worth it. If 2+ FT, be proactive.
4. CHIPS: Only recommend chips marked AVAILABLE. Never recommend a chip that has been played.
5. CONFIDENCE must be close to {conf_hint}/10 — adjust ±0.5 based on fixture clarity and squad depth, but anchor near {conf_hint}.
6. CAPTAIN must be a player already in the squad.

Reply using EXACTLY these six headers with nothing before or after:

TRANSFER OUT
[Player name (Team) £Xm] — [specific reason: form score, fixture run, injury, or price risk]
OR: Hold — [why no move is needed given their budget of £{bank:.1f}m and {ft} FT]

TRANSFER IN
[Player name (Team) £Xm] — [form + fixture reason. Confirm it fits £{bank:.1f}m bank]
If hit required: "Requires 4pt hit — worth it because [specific reason]"
OR: N/A

CAPTAIN
[Player name (Team)] — beat [2 rivals from the squad] because [specific fixture/form reason]

CHIP
[Chip name] — [specific GW or trigger justifying playing NOW]
OR: Hold [chip name] — [specific better moment e.g. confirmed DGW around GW32]

CONFIDENCE
{conf_hint} / 10 — [one-line reason matching the squad situation]

SUMMARY
[Sentence 1: single most urgent action this GW]
[Sentence 2: what to watch before next deadline]

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
