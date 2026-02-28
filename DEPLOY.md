# Gaffer Backend — Phase 1 Deployment Guide

## What you are deploying

A Python FastAPI server that:
- Proxies FPL data (bypasses browser CORS so real team stats appear on the landing page)
- Generates gameweek recommendations using Claude
- Sends briefing emails via Resend 24 hours before each deadline
- Stores users in Supabase

## Services needed (all free tiers)

| Service | Purpose | Cost |
|---|---|---|
| Railway | Hosts the Python server | Free (then $5/month) |
| Supabase | Stores user data | Free forever for this scale |
| Resend | Sends briefing emails | Free (3,000/month) |
| Anthropic | Powers AI recommendations | ~$0.01 per briefing |

---

## Step 1 — Supabase setup (5 minutes)

1. Go to supabase.com and create a free account
2. Create a new project — name it "gaffer"
3. Once created, go to the SQL Editor
4. Run this query to create the users table:

```sql
create table users (
  id          bigserial primary key,
  team_id     integer unique not null,
  team_name   text,
  email       text,
  mode        text default 'assisted',
  active      boolean default true,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

create index on users(team_id);
create index on users(email);
```

5. Go to Project Settings > API
6. Copy your Project URL and anon public key — you will need these

---

## Step 2 — Resend setup (5 minutes)

1. Go to resend.com and create a free account
2. Go to API Keys and create a new key
3. Copy the key — starts with re_

For Phase 1, you can send from onboarding@resend.dev (their test domain).
To send from your own domain later, add a DNS record — Resend walks you through it.

---

## Step 3 — Push to GitHub (2 minutes)

1. Create a new repo at github.com — name it "gaffer-backend"
2. In your gaffer-backend folder, run:

```bash
git init
git add .
git commit -m "Gaffer Phase 1 backend"
git remote add origin https://github.com/YOURUSERNAME/gaffer-backend.git
git push -u origin main
```

---

## Step 4 — Deploy on Railway (5 minutes)

1. Go to railway.app and create a free account
2. Click New Project > Deploy from GitHub repo
3. Select your gaffer-backend repo
4. Railway will detect the Procfile and start deploying

Once deployed, go to Variables and add these environment variables:

```
ANTHROPIC_API_KEY     = sk-ant-...
RESEND_API_KEY        = re_...
EMAIL_FROM            = Gaffer <briefing@resend.dev>
SUPABASE_URL          = https://yourproject.supabase.co
SUPABASE_API_KEY      = your-anon-key
```

5. Railway will give you a public URL like:
   https://gaffer-backend-production.up.railway.app

---

## Step 5 — Connect the frontend

Open gaffer/src/gaffer.js and update line 1:

```javascript
const API_BASE = 'https://YOUR-RAILWAY-URL.up.railway.app';
```

Re-deploy your Gaffer landing page on Vercel. Real FPL data will now populate when someone enters their team ID.

---

## Step 6 — Test it

1. Visit your Gaffer landing page
2. Enter your own FPL team ID and email
3. Click Connect
4. Your real stats should appear within 2 seconds
5. Check your email — you should receive a briefing

---

## Testing the API directly

Once deployed you can test endpoints directly:

```bash
# Check server is up
curl https://YOUR-URL.up.railway.app/

# Get team data
curl https://YOUR-URL.up.railway.app/team/YOUR_TEAM_ID

# Get a full briefing
curl https://YOUR-URL.up.railway.app/briefing/YOUR_TEAM_ID
```

---

## Monthly costs at scale

| Users | API cost | Resend | Railway | Supabase | Total |
|---|---|---|---|---|---|
| 50 | ~$2 | Free | Free | Free | ~$2 |
| 200 | ~$8 | Free | $5 | Free | ~$13 |
| 500 | ~$20 | $20 | $5 | Free | ~$45 |

At 500 Pro users paying $9/month = $4,500 revenue vs ~$45 costs.
