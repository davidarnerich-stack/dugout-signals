# Dugout Signals — Deployment Guide

## What you need
- GitHub account (free)
- Render.com account (free to create, $7/mo for the Starter plan)
- Your Cloudflare account with dugoutsignals.com

---

## Step 1 — Push to GitHub

```bash
cd ~/Desktop/dugout-signals-web
git init
git add .
git commit -m "Initial commit"
```

Then go to github.com → New repository → name it `dugout-signals` → Create.
Copy the two commands GitHub shows you ("push an existing repository") and run them.

---

## Step 2 — Deploy on Render

1. Go to **render.com** → sign in → **New** → **Web Service**
2. Connect your GitHub account and select the `dugout-signals` repo
3. Render will auto-detect the settings from `render.yaml`. Confirm:
   - **Runtime**: Python
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app --timeout 120 --workers 2`
   - **Plan**: Starter ($7/mo) — avoids cold starts
4. Under **Environment Variables**, add these manually:
   - `APP_PASSWORD` → choose your password (e.g. `Storm2026!`)
   - `TEAM_NAME` → the team this login is scoped to (e.g. `Storm 12U All-Stars`) — until DS-14 (per-coach accounts) ships, this is the one team every login sees
   - `SUPABASE_KEY` → paste your Supabase anon key
   - `SUPABASE_URL` is already set in render.yaml
5. Click **Create Web Service**

Render will build and deploy. Takes ~3 minutes. You'll get a URL like:
`https://dugout-signals.onrender.com`

Test it there before setting up the custom domain.

---

## Step 3 — Custom Domain on Render

1. In Render → your service → **Settings** → **Custom Domains**
2. Click **Add Custom Domain**
3. Type: `upload.dugoutsignals.com` (or just `dugoutsignals.com`)
4. Render will show you a CNAME value to add in Cloudflare

---

## Step 4 — Cloudflare DNS

1. Log in to **Cloudflare** → select `dugoutsignals.com` → **DNS**
2. Click **Add Record**:
   - **Type**: CNAME
   - **Name**: `upload` (makes it `upload.dugoutsignals.com`)
   - **Target**: the value Render gave you (e.g. `dugout-signals.onrender.com`)
   - **Proxy status**: ☁️ **Proxied** (orange cloud — ON)
3. Click Save

Then in Cloudflare → **SSL/TLS** → set mode to **Full** (not Flexible, not Full Strict).

DNS propagates in ~2 minutes through Cloudflare. Render will auto-provision SSL.

---

## Step 5 — Test

Go to `https://upload.dugoutsignals.com`
- You should see the Dugout Signals login page
- Enter the password you set in Step 2
- Upload a Stats CSV → should show green success with player names

---

## Future game uploads

1. Go to `https://upload.dugoutsignals.com`
2. Drag in the new files (can drop all 3 file types at once)
3. Click Upload & Process
4. Done — data is live in Supabase

The upload order matters if it's a brand-new game:
**Stats CSV first** → Box Score PDF → Play-by-Play DOCX

(Box Score and Play-by-Play look up the game_id created by the Stats CSV.)

## Marketing site (dugoutsignals.ai)

Deploys from this repo too, on the same `git push origin main`. Cloudflare
Pages is connected to the GitHub repo; there is no manual upload step any more.

| Setting | Value |
|---|---|
| Build command | `cp static/tokens.css marketing/tokens.css` |
| Deploy command | `npx wrangler deploy` |
| Path | `/` |

Which directory is published is set in [wrangler.jsonc](wrangler.jsonc)
(`assets.directory`), not in the dashboard. This is Cloudflare **Workers**,
not Pages — the Workers flow has no "Build output directory" field.

`static/tokens.css` is the one design-token file shared by the app and the
marketing site. The build copies it into the marketing output because a static
site on another host cannot read Flask's static folder at runtime. Change
colors there and both surfaces move together.

See [marketing/README.md](marketing/README.md) for the full picture.
