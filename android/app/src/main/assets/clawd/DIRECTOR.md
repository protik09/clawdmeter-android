# Which Clawd animation plays when (from Clawdmeter firmware: clawd_director/policy, mascot, performer, face)
Names are manifest keys. Only one Clawd is ever on screen; he enters and leaves through an edge using `walking` (loop 2..6 while moving, mirrored when heading left). Every act ends on `still` (= walking frame 0).
**Mood group** = max(usage-rate group, hook activity `a`). Rate = session %/h over the last 15 min: <5 Idle, <10 Normal, <20 Active, >=20 Heavy. An `a` level expires 3 min after it was last sent.

- **Idle (home):** sits on `still` in his corner. After a still time of 10/7/5/3.5 s (Idle/Normal/Active/Heavy, ±40 % jitter, scaled by `lv`, min 1.5 s) he plays one act, never the same twice running:
  Idle `pointing`/lurk · Normal `waving`/lurk/`pointing` · Active `waving`/`dancing`/lurk · Heavy `dancing`/`waving`/`dancing`/lurk.
  Lurk = walk off left, then `lurking` (full size, mirrored) peeks in from the right edge, then walks back in.
- **Working (`a` 1–3):** raises the mood group (busier acts). `a`=3 held 5 min (cooldown 15 min) earns a **show**: `pointing`, walks off left at 1.5x, walks back on full size, plays one scene with its loop held 16 s (~20 s total), `waving`, walks off, corner Clawd walks home.
  Scene set: usage-rate group floored at Active. Active: `laptop`/`dancing`/`skateboard`/`soccer`; Heavy (real burn): `racing_car`/`cloud`/`sailing_scene`/`jumping_happy`. No repeat of the previous show.
- **Prompt `e:"p"` / back-after-gap `e:"b"`:** `pointing` once (anticipation). If the screensaver face is up, it blinks, drops and he walks home at 1.5x. Also wakes a dimmed screen.
- **Stop `e:"s"`:** `waving` once, only if the turn (first prompt to stop) ran >= 2 min. Shorter turns get nothing.
- **Attention (`b` chime + `m` message, `x` clears):** the corner Clawd walks off at 2x, then a bigger Clawd (1.5x cell size) walks on at 2x and alternates `waving` and `pointing` until a tap, `x`, or the 2 min timeout (setting `aa`: 0/10/30/120 s of animation; afterwards he just sits). Then he walks off left and the corner Clawd returns. Minimal mode holds `pointing` frame 16.
- **High usage / limited:** no dedicated animation. `st:"limited"` and 100 % aren't used by Clawd; high usage only pushes the mood to Heavy (more frequent acts, the dancing-heavy act set, the Heavy show set). The bars just turn red.
- **Window reset** (a real sample whose session % drops >5 points): `jumping_happy` x2 with the chime. If he's out he hurries back, and it waits up to 5 min for him.
- **Tap on meters (peek):** `jumping` from frame 1 at 2x (startled hop), he exits at 4x, then the face rises and looks toward the tap.
- **Screensaver** (10 min with no hook event, tap, button, alert or usage rise): walks off, then the face rises. Every 3–5 min a calm dip: the face drops, he walks on, plays `magnifier` or `walking` (alternating, loop held 10 s), walks off, and the face rises again.
- **Never chosen by the director:** `crab_walking`, `trumpet`, `basketball` (listed in the Normal performer group, but shows start at Active).

## The face (procedural, no sprite)
Full-screen flat rectangle in the Clawd colour (#D97757) with a flat top edge, and two solid eye rectangles in the eye colour (#141413).
Geometry is designed for a 480 px square (scale by min(W,H)/480): eyes 44x120 px, centres ±80 px from the middle, eye top at y 150; arm nubs 48x56 at y 240, shown only while it moves.
**Blink:** each eye is replaced by a squint chevron: left `>`, right `<`. Each is 9 blocks of 2u x u (u = 12 px) stepping x by 0,1,2,3,4,3,2,1,0 u down 9 rows, starting at y+20. Closed 180 ms; a double blink is open 140 ms, then closed 180 ms again.
**Resting rhythm:** every 2–6 s (random): 45 % blink, 12 % double blink, 22 % glance left, 21 % glance right. A glance moves the eyes 20 px (the far eye 30 px), easing out over 280 ms, holds 0.7–1.6 s, then comes back.
**Rise** 860 ms: ease-out from below the bezel to 44 px past rest at 500 ms, then a spring settle (period 300 ms, decay 110 ms). **Drop** 510 ms: a 150 ms crouch (6 px down) and hop (28 px up), then an ease-in fall. The eyes lag the body by 40 ms.
**Peek** (after a tap): eyes glance toward the tap x (proportional, up to 30 px), hold 1.6–2.4 s, blink (30 % double). It stays up for 5 min, with 60 % of glances going back toward the tap. Another tap blinks, drops it and sends him home.

## Liveliness `lv` (1–5, default 3)
1: no shows, screensaver, idle acts, lurks or reactions (point, wave, joy); taps and alerts still work. 2: timings ÷0.6. 3: base. 4: ÷1.8. 5: ÷3, and shows are earned at `a`>=2.
Scaled: show-after (5 min), show cooldown (15 min), calm-dip interval (3–5 min), corner still time, long-turn threshold (2 min). Not scaled: the 10 min screensaver quiet time.
