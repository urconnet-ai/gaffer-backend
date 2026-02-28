"""
Gaffer — scheduler.py
Sends briefing emails to all registered users before each gameweek deadline.

Run alongside the API server:
  python -m app.scheduler

How it works:
  - Checks FPL for the next deadline every 30 minutes
  - When 24 hours remain, sends briefings to all active users with an email
  - Logs delivery for each user

For Railway deployment, run this as a separate process or use Railway's cron feature.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import httpx

from app.fpl import get_gameweek_info, get_team, get_player_data, get_fixtures
from app.analysis import generate_recommendation
from app.notifications import send_briefing_email
from app.database import get_all_active_users

CHECK_INTERVAL_MINUTES = 30
BRIEFING_WINDOW_HOURS  = 24   # Send briefing this many hours before deadline


async def run():
    print("[scheduler] Started — checking every 30 minutes")
    already_sent_gw = None

    while True:
        try:
            gw_info  = await get_gameweek_info()
            next_gw  = gw_info.get("next") if gw_info else None

            if next_gw:
                deadline_str = next_gw.get("deadline_time")
                gw_name      = next_gw.get("name")

                if deadline_str:
                    deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                    now      = datetime.now(timezone.utc)
                    hours_remaining = (deadline - now).total_seconds() / 3600

                    print(f"[scheduler] {gw_name} deadline in {hours_remaining:.1f} hours")

                    # Send when inside the 24hr window, but only once per GW
                    if hours_remaining <= BRIEFING_WINDOW_HOURS and already_sent_gw != gw_name:
                        print(f"[scheduler] Sending briefings for {gw_name}")
                        await send_all_briefings(gw_info)
                        already_sent_gw = gw_name
                    else:
                        print(f"[scheduler] Outside briefing window or already sent — waiting")

        except Exception as e:
            print(f"[scheduler] Error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


async def send_all_briefings(gw_info: dict):
    """Fetches all active users and sends each a briefing."""
    users    = await get_all_active_users()
    players  = await get_player_data()
    fixtures = await get_fixtures()

    print(f"[scheduler] Sending to {len(users)} users")

    for user in users:
        if not user.get("email"):
            continue
        try:
            team_data = await get_team(user["team_id"])
            if not team_data:
                continue

            rec = await generate_recommendation(team_data, gw_info, players, fixtures)
            await send_briefing_email(user["email"], user.get("team_name", "Your Team"), rec)
            print(f"[scheduler] Sent to {user['email']} (team {user['team_id']})")

            # Polite rate limiting — don't hammer Anthropic or Resend
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"[scheduler] Error for team {user.get('team_id')}: {e}")


if __name__ == "__main__":
    asyncio.run(run())
