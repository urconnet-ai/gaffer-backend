"""
Gaffer — notifications.py
Email delivery via Resend (resend.com).
Free tier: 3,000 emails/month — more than enough for Phase 1.
"""

import os
import httpx

RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_briefing_email(to_email: str, team_name: str, rec: dict):
    """Sends a formatted gameweek briefing email via Resend."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        print("[email] RESEND_API_KEY not set — skipping email delivery")
        return

    subject = f"Gaffer — {rec.get('gameweek', 'GW')} Briefing | {team_name}"
    html    = build_email_html(team_name, rec)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from":    os.getenv("EMAIL_FROM", "Gaffer <briefing@gaffer.app>"),
                "to":      [to_email],
                "subject": subject,
                "html":    html,
            }
        )

        if response.status_code not in (200, 201):
            print(f"[email] Resend error {response.status_code}: {response.text}")
        else:
            print(f"[email] Briefing sent to {to_email} for {team_name}")


def build_email_html(team_name: str, rec: dict) -> str:
    """Renders the briefing email as clean HTML."""

    def row(label: str, content: str, color: str = "#888") -> str:
        lines = content.replace("\n", "<br>")
        return f"""
        <tr>
          <td style="padding:14px 20px;border-bottom:1px solid #1e1e1e;">
            <div style="font-size:10px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:{color};margin-bottom:6px;">{label}</div>
            <div style="font-size:14px;color:#cccccc;line-height:1.65;">{lines}</div>
          </td>
        </tr>"""

    transfer_out_color = "#ff4545"
    transfer_in_color  = "#c8f135"
    captain_color      = "#f5a623"
    chip_color         = "#38bfff"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 20px;">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="background:#111111;border:1px solid #222222;max-width:580px;">

        <!-- Header -->
        <tr>
          <td style="padding:24px 20px 20px;border-bottom:2px solid #c8f135;">
            <div style="font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#888;margin-bottom:4px;">Gaffer</div>
            <div style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:-0.5px;">{rec.get('gameweek','GW')} Briefing — {team_name}</div>
            <div style="font-size:12px;color:#555;margin-top:4px;">Deadline: {rec.get('deadline','')}</div>
          </td>
        </tr>

        <!-- Content rows -->
        <table width="100%" cellpadding="0" cellspacing="0">
          {row("Transfer Out", rec.get("transfer_out", ""), transfer_out_color)}
          {row("Transfer In",  rec.get("transfer_in",  ""), transfer_in_color)}
          {row("Captain",      rec.get("captain",      ""), captain_color)}
          {row("Chip Advice",  rec.get("chip",         ""), chip_color)}
          {row("Confidence",   rec.get("confidence",   ""), "#888")}
          {row("Summary",      rec.get("summary",      ""), "#ffffff")}
        </table>

        <!-- CTA -->
        <tr>
          <td style="padding:20px;text-align:center;border-top:1px solid #222;">
            <a href="https://gaffer.app" style="display:inline-block;padding:12px 32px;background:#c8f135;color:#0a0a0a;font-size:13px;font-weight:700;letter-spacing:0.5px;text-decoration:none;">View Full Analysis →</a>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:14px 20px;border-top:1px solid #1a1a1a;">
            <div style="font-size:11px;color:#444;">Gaffer FPL Co-Manager · <a href="https://gaffer.app/unsubscribe" style="color:#444;">Unsubscribe</a></div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
