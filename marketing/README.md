# Marketing site — dugoutsignals.ai

The public landing page. Lives in this repo on purpose: it used to be a folder
on David's Desktop that got dragged into the Cloudflare Pages dashboard, which
meant production was whatever was last uploaded, nothing was reviewable or
revertable, and an incomplete upload shipped a broken page with no warning.
That happened on 2026-08-24 — an HTML-only upload took the logos down.

## Deploy

`git push origin main` deploys this, the same push that deploys the app.
Cloudflare Pages is connected to the GitHub repo and builds on push.

Cloudflare Workers Builds settings (dashboard):

| Setting | Value |
|---|---|
| Repository | `davidarnerich-stack/dugout-signals` |
| Production branch | `main` |
| Build command | `cp static/tokens.css marketing/tokens.css` |
| Deploy command | `npx wrangler deploy` |
| Path | `/` |

Note the build command takes a **space**, not a comma — `cp SOURCE DEST`.

Everything else — crucially *which directory gets published* — lives in
[`wrangler.jsonc`](../wrangler.jsonc) at the repo root, not in the dashboard.
That is deliberate: it is reviewable and revertable there.

This is Workers, not Pages. Cloudflare steers new git connections to Workers
now, and the Workers flow has no "Build output directory" field — that concept
is `assets.directory` in `wrangler.jsonc` instead.

`.assetsignore` keeps this README out of the published output.

Nothing is dragged into a dashboard any more. If a file is in this folder and
committed, it ships; if it is not, the build is identical to what you can see
in git.

## Why the build command exists

`static/tokens.css` is the single source of truth for colors, type, and radii,
shared with the Flask app. The app serves it at `/static/tokens.css`. This is a
static site on a different host and cannot read Flask's static folder at
runtime, so the build copies that exact file into the published output.

One physical file, copied at deploy. No sync script, no diff test, and no way
for the two surfaces to hold different values — the failure mode is a missing
file (obvious, immediate) rather than two files quietly disagreeing.

`marketing/tokens.css` is gitignored. Do not commit it: a committed copy is a
second source of truth, which is the problem this removed.

## Working on it locally

    cp static/tokens.css marketing/tokens.css   # same thing the build does
    python3 -m http.server 8901 --directory marketing

Then open http://localhost:8901. Colors come from `static/tokens.css`; change
them there, not here.

## The waitlist form

Posts to `POST /api/waitlist` on the app (DS-128) and shows its success state
only on a 2xx. It previously fired a `mailto:` and claimed success
unconditionally, telling people they had joined a list that did not exist.
If you touch `handleWaitlist()`, keep that contract.
