# Voiceover script — clean narration only

Paste each block into ElevenLabs separately, or the whole thing at once if you
prefer a single take. No stage directions, no markdown — it will read punctuation
aloud if you leave it in.

Suggested voice: a calm, mid-paced male or female narrator. Avoid the
over-enthusiastic "explainer" presets; the numbers are dramatic enough and an
excited read makes them sound like marketing.

Total: roughly 2 minutes 40 seconds at a natural pace, leaving headroom under the
3-minute limit.

---

## Block 1 — Hook (0:00–0:20)

This is Paritok's own dashboard number for a coding agent session. It reports
saving sixty-four percent of my input tokens.

And this is what the provider actually billed for that same session. Sixty-nine
percent more than if I'd sent the file with no compression at all.

Same session. Both numbers are real. I'm going to show you why.

---

## Block 2 — Mechanism (0:20–0:55)

Paritok compresses your context and forwards it. But when the model needs the
exact original, it calls expand context. And the proxy answers that itself, by
appending the full file to the request and sending it upstream a second time.

That's this row. Twenty-seven thousand tokens. The provider bills it.

Paritok's stats are computed once, before that loop runs. So it never sees this
POST. It reports the first one, and stops counting.

---

## Block 3 — Live demo (0:55–1:35)

This is running two real Paritok proxies, right now. One stock, one patched. It
drives an actual multi-turn session through each, and counts every upstream POST.
Compression is happening on Paritok's hosted four-B model.

Stock Paritok: six POSTs. A hundred and four thousand tokens billed.

---

## Block 4 — The fix (1:35–2:15)

The fix is one idea. When the model calls expand context, it's telling you that
compressing that content was a mistake. That's a free supervision signal, and
nothing uses it.

So: pin it. Pass that content through verbatim from then on. The reference never
comes back, so there's nothing left to expand.

Four POSTs instead of six. Twenty-nine percent fewer billed tokens. Two fewer
round trips.

And when the model never expands, the two are identical. Pinning costs nothing
when it never fires.

---

## Block 5 — Honesty (2:15–2:45)

Two things I want to be straight about.

This doesn't make that session profitable. It goes from seventy-four percent
overspend, to twenty-three. The pin is learned from the first expansion, so the
first turn is always paid in full.

And I haven't modelled prompt caching, which would lower the absolute numbers.
Though not the direction.

---

## Block 6 — Close (2:45–3:00)

Everything's Apache two point oh, and every number reproduces from the repo. Real
proxies, the released package, the real model.

Paritok built a dial for this. It just doesn't have a controller yet.

---
---

# Shot list

What to have on screen for each block. Record these as separate clips — far
easier to trim than one long take.

| Block | On screen | Capture notes |
|---|---|---|
| 1 | The reconciliation panel, both big numbers visible | Have a finished run already loaded. This is the money shot — hold it still. |
| 2 | Scroll slowly to the POST ledger; cursor rests on the "not counted" row | Slow scroll. Let the tag be readable. |
| 3 | Paste a file, click Run, stages ticking | Record ~15s of real stages, then cut. Do NOT film the full 110s wait. |
| 4 | The comparison bars, then the stat grid | Let the bars finish animating before cutting. |
| 5 | The limitations text at the bottom of the results | Slow scroll, no cursor movement. |
| 6 | The GitHub repo page | Just the README top, license badge visible. |

## Before you record

1. **Load the app once to wake it** — the free tier sleeps after 15 minutes idle.
2. **Run a full session first** so block 1 has real results on screen.
3. **Dark theme** — the reconciliation reads harder against it.
4. **Browser zoom ~125%** so figures are legible at video resolution.
5. Close other tabs, hide bookmarks bar, full-screen the window.

## Assembly

- Match each clip to its audio block; trim video to the narration, not the
  reverse.
- No background music. The numbers carry it; a soundtrack makes it an advert.
- Export 1080p, upload unlisted to YouTube, paste the link into Devpost.
