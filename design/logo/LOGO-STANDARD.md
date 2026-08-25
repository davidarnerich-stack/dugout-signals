# Dugout Signals — Logo assets (canonical)

Approved 31 Jul 2026. **Seam revision approved 25 Aug 2026** — see *The seam* below.
Preview and rationale: **`Logo Exploration v2.dc.html`**; the seam revision and the options it
was chosen from: **`Logo Stitch Revision.dc.html`** (option 4A shipped).

## The mark

A ball broadcasting signal arcs. The dugout is the idea, not a drawn object — the earlier
concepts (a molecule, and a literal dugout with radio waves) are compared in the exploration file.

Pure geometry: circles, arcs and paths. Self-contained, no embedded raster, no font dependency.
Renders anywhere including inside `<img>`, and scales to any size.

## The seam — direction is a rule, not a detail

Revised 25 Aug 2026. The original seam was wrong in three ways at once, and all three are fixed.

**1 · Direction.** On a real ball the two visible seams curve *toward each other* — endpoints out
near the silhouette at top and bottom, waist pulled in toward the middle:

    correct  ) (          wrong  ( )

The original mark had it inverted. If a future edit makes the seams bow apart, it is wrong —
check it against a photograph, not against intuition.

**2 · Reach.** Seam endpoints sit **on** the ball circle, 30° off the poles
(`50.5,43.5` and `50.5,76.5` for r=19 at 60,60). The original stopped ~5 units short and read
as a decal floating on the ball.

**3 · Axis.** The seam group is rotated **40°** — `<g transform="rotate(40 60 60)">`. Vertical
seams ran parallel to the vertical sport split, stacking three near-parallel lines inside a 38px
ball; nothing told the eye which line was structure and which was sport. 40° also clears the
signal arcs' horizontal axis.

**Stitches** are chevron pairs straddling the seam, spaced by arc length and foreshortened toward
the poles so they thin out at the silhouette instead of stopping flat. They are the seam — the
drawn seam line underneath is a faint groove at 42%, not a red stripe.

Geometry is stored once in **`_stitch-geometry.json`** (axis, seam curves, both stitch sets).
Regenerate from there rather than hand-editing path data.

## Sport is the ball, and only the ball

Every file below is byte-identical apart from the ball fill. A real softball has red stitching too,
so the seams never change — which means the sport switch is one attribute, not one logo.

| File | Ball | Use |
|---|---|---|
| `dugout-signals-mark.svg` | split white / optic yellow | **Default.** Marketing site, pre-setup, anywhere sport is unknown |
| `dugout-signals-mark-baseball.svg` | white `#e6edf3` | In-product once a team picks baseball |
| `dugout-signals-mark-softball.svg` | optic yellow `#facc15` | In-product once a team picks softball |
| `dugout-signals-mark-mono.svg` | outlined, one ink `#0d1117` | Print, light backgrounds, embroidery |
| `dugout-signals-mark-nav.svg` | split, 5 heavier stitch pairs | **Nav / header at 40px** — what `logo-icon.svg` ships as |
| `dugout-signals-mark-small.svg` | solid, no seams | Anything under 24px where the ball must still be a ball |
| `dugout-signals-favicon.svg` | ball only, tight crop | Browser tab — see *Favicons* |

## Size tiers

Detail drops as the mark shrinks. This is deliberate — it's how the mark earns the right to be used
at 20px. The tiers changed with the seam revision: stitch count and stroke weight now step
together, because a 1.15 stroke in a 120 viewBox is sub-pixel below ~64px and anti-aliases to grey.

- **64px and up** — 8 stitch pairs at 1.15, seam groove at 42%. The main files
- **28–63px** — 5 pairs at 1.9, groove at 50%. Use `dugout-signals-mark-nav.svg`
- **under 24px** — solid ball, no seams. Use `dugout-signals-mark-small.svg`

## Favicons

The full mark does not survive a browser tab — at 16px the ball is 5px across and the arcs are the
whole icon. So the favicon is a **different drawing of the same idea**: the ball alone, cropped
tight to the frame, with 5 heavy stitch pairs on the same 40° axis. The split white/optic-yellow
is what keeps it from being a generic baseball, and it is legible at 16px.

| File | Size | Use |
|---|---|---|
| `dugout-signals-favicon.svg` | 32 viewBox, scalable | `rel="icon" type="image/svg+xml"` — modern browsers |
| `favicon-32.png` | 32×32 | `rel="icon" sizes="32x32"` fallback |
| `favicon-16.png` | 16×16 | `rel="icon" sizes="16x16"` fallback |
| `apple-touch-icon.png` | 180×180 | iOS home screen — the **full** mark, arcs included, on a `#0d1117` rounded field |

180px has room for the whole mark, so the touch icon is not the cropped ball. Link order matters:
SVG first, PNGs after, so browsers that support SVG icons take the scalable one.

No `.ico` is shipped. Every browser that matters reads PNG and SVG icons; `favicon.ico` is only
needed for IE11.

## The lockup

Full logo = mark **+** wordmark. The wordmark is **live type, never baked into the SVG** — SVG
`<text>` won't render Orbitron inside an `<img>`.

- Wordmark: **Orbitron 700**, "DUGOUT SIGNALS", letter-spacing `.1em`
- "DUGOUT" `#f0f6fc` · "SIGNALS" `#38bdf8` (or both `#0d1117` on light)
- Mark height ≈ 2.6× the wordmark cap height; gap ≈ 10px at a 40px mark
- Tagline (optional): Inter 600, 9–10px, letter-spacing `.18em`, uppercase, `#8b949e`

## Colors

Arc sky `#38bdf8` (opacity steps .85 / .55 / .3 outward) · seam red `#c9403a` ·
baseball `#e6edf3` · softball `#facc15` · mono ink `#0d1117`. Always transparent background.

## Where these go

Both codebases already reference these filenames, so the fix is a file swap — see
`deliverables/` at the project root.

| Repo / host | Path | Canonical source |
|---|---|---|
| Flask app | `static/logo-icon.svg` | `dugout-signals-mark.svg` |
| Flask app | `static/Logo_Dugout-Signals__baseball_-transparent.svg` | `…-baseball.svg` |
| Flask app | `static/Logo_Dugout-Signals__softball_-transparent.svg` | `…-softball.svg` |
| Cloudflare static | `logo-icon.svg` | `dugout-signals-mark.svg` |
| Cloudflare static | `Logo_Dugout-Signals__baseball-softball_.svg` | `dugout-signals-mark.svg` |

## What was here before

`source-from-repo/` holds the previous artwork for reference: the molecule logo as raster
(`logo.jpg`, `logo-banner.png`) and `logo-icon-BROKEN.svg`.

**Every SVG in the previous set was an empty shell** — an `<image>` element with no image data —
so both the app header and the marketing homepage were rendering nothing. That is what these files
fix. The molecule artwork only ever existed as raster.
