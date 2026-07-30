# Devpost submission copy

Paste-ready. Fill the two bracketed fields before submitting.

---

## Tagline (one line)

Paritok told me it saved 64% on a session that billed 69% more than no
compression at all. Here's the measurement, and the fix.

---

## Links

- **Live demo:** https://pin-on-expand.onrender.com
- **Repository:** https://github.com/GauravGupte20/paritok-pin-on-expand
- **Video:** [YOUTUBE/VIMEO URL]
- **Paritok account email:** [YOUR PARITOK SIGNUP EMAIL]
- **License:** Apache 2.0

---

## Inspiration

Paritok's roadmap lists adaptive compression — per-segment aggressiveness chosen
by age, kind and intent — as an open problem. I went looking for a policy signal
nobody was using.

I found one: when the model calls `expand_context`, it is *telling you* that
compressing that content was a mistake. That is free supervision, and nothing
consumes it.

But building the harness to measure whether acting on that signal helped turned
up something bigger, and the project became about that instead.

## What it does

**The finding.** When the model calls `expand_context`, the proxy resolves it
inside its own loop, appends the full original to a proxy-local thread, and POSTs
that thread upstream again. `stats` is computed once in `process_request` —
*before* that loop runs. So the second POST is billed by the provider and never
reaches Paritok's accounting.

There is a second effect. `_conceal_virtual_calls` strips the exchange from the
reply, so the agent never sees it. Next turn the agent re-sends the original,
Paritok re-compresses it to the same `[REF:id]`, and the model expands it again —
every turn, forever.

Measured on one client turn through a real `paritok proxy`, compressing on
Paritok's hosted 4B GPU, with `argparse.py` (20,005 tokens) in context:

```
post 0:  6,919 tokens   compressed request        ← counted
post 1: 26,924 tokens   the full expanded original ← not counted
                        ─────────
billed:  33,843
```

`/stats` reports **64.0% saved**. The provider billed **69.2% more** than sending
the file with no compression at all. 26,639 tokens were billed and never counted.

**The fix — pin-on-expand.** Treat the expansion as an admission: pin that
content and pass it through verbatim from then on. The `[REF:id]` never
reappears, so there is nothing left to expand, and the turn costs one POST
instead of two. Pins are keyed by content hash and source path. Content nobody
expands is compressed exactly as before.

A/B over 3 turns, both real proxy processes, hosted GPU:

| Model never expands *(control)* | POSTs | Billed | vs no proxy |
|---|--:|--:|--:|
| stock Paritok | 3 | 20,787 | +65.4% |
| pin-on-expand | 3 | 20,787 | +65.4% |

| Model expands each turn | POSTs | Billed | vs no proxy |
|---|--:|--:|--:|
| stock Paritok | 6 | 104,229 | −73.7% |
| pin-on-expand | 4 | 73,901 | −23.1% |

**29.1% fewer billed tokens, 2 fewer upstream round-trips.** The control is a
dead heat — pinning costs nothing when it never fires. It narrows compression
only where the model has demonstrated compression was wrong.

**The app.** Paste any source file at the live demo. It cold-starts two real
`paritok proxy` processes — one stock, one patched — drives a real multi-turn
session through each over HTTP, and shows the reconciliation: every upstream
POST, which ones `/stats` counted, and what the bill actually was.

## How I built it

- `paritok_adaptive/pin.py` — `PinningPipeline` and `PinningEngine`. `install()`
  rebinds `ParitokEngine` before `proxy/server.py` constructs it, so no fork of
  Paritok is required.
- A replay harness that drives scripted multi-turn sessions through the real
  engine, with a deterministic compressor so runs are reproducible and free.
- A mock provider that records every upstream POST. The provider has to be mocked:
  a real one will neither let you script when the model calls `expand_context`
  nor tell you exactly what each POST was billed, and both *are* the measurement.
- A FastAPI app that manages the proxy subprocesses. Runs are asynchronous —
  they take ~110s on the hosted GPU, longer than a platform holds an HTTP request
  open — so the endpoint returns a job id and the UI polls the server's real
  progress rather than faking it on a timer.

Every run cold-starts its proxies. The compression cache, shadow store and pin
set all live for the life of the process and `/stats` is cumulative, so a warm
proxy silently returns a previous run's result.

## Challenges

**Nearly shipping a wrong number, four times.** My first synthetic fixture had 26
near-identical functions, which `deduplicate_definitions` collapsed to almost
nothing — a fake 95% compression rate. My SEG-parsing regex matched the literal
`[SEG]` in the prompt's *instruction text*, so every segment compressed to `...`.
I used `wrapper.py` as a fixture without noticing it contains the strings
`expand_context` and `[REF:`, which fired my own detection logic. And the proxy's
in-memory cache survived a fix, making it look like the fix hadn't worked. Three
of those four would have produced a confidently wrong headline.

**My first reproduction was right about the wrong thing.** I confirmed an
expand→re-collapse loop before realising `_conceal_virtual_calls` means that
applies to SDK mode only. In proxy mode the failure is the accounting gap plus
per-turn re-expansion. I had to throw out the model and re-derive it against
`server.py`.

**Deploying on 0.1 CPU.** `/api/run` returned 500 while the page served fine. Not
memory — forking Python and importing paritok takes ~25s at that CPU share, past
my startup timeout, and a full run outlives the platform's request timeout. Fixed
by making runs asynchronous.

## Accomplishments

The claim survives every check I could throw at it: real proxy processes, the
released PyPI package rather than a checkout, Paritok's real hosted 4B model, and
a Docker image that reproduces it from scratch. All four source line references
in the README were verified to land on the cited code in shipped 1.2.8.

And the fix does *better* against the real model than against the stand-in —
29.1% versus 22.6%.

## What I learned

Measurement infrastructure is the deliverable. The fix is about 120 lines; the
harness that proves it isn't lying is most of the repo. Every serious bug I hit
was in my own measurement, not in Paritok.

Also: `/stats` isn't wrong so much as scoped. Paritok's README says it covers
"what Paritok actually intervenes in." The problem is narrower — the excluded
traffic is *generated by the gateway itself*, which is the one category a user
would most want counted.

## What's next

The `level` parameter (L0–L3) is fully implemented in Paritok but nothing selects
between values — tool results always take the default L1, history is hardcoded to
L3. Pin-on-expand is the binary case of a policy that should be graded. The dial
already exists; it just needs a controller.

Also open: modelling prompt caching, which would lower the absolute overspend
figures without changing their direction.

## Built with

`python` · `paritok` · `fastapi` · `uvicorn` · `httpx` · `tiktoken` · `docker` ·
`render`

## Notes filed for the Paritok team

1. `/stats` excludes resolve-loop traffic the proxy itself generates.
2. `served_refs` dedupes expansions within a turn but not across turns, and
   `_conceal_virtual_calls` guarantees nothing carries over.
3. The L0–L3 dial is implemented but unused — adaptive compression is mostly a
   policy question, not a modelling one.

---
---

# Video script — under 3 minutes

Record the live app at https://pin-on-expand.onrender.com. **Warm it up first**
(free tier sleeps after 15 min idle) and **start a run before recording** so you
can cut to the finished result instead of waiting ~110 seconds.

---

**0:00–0:20 — The hook**

> *[Screen: the app's reconciliation panel, both numbers visible]*
>
> "This is Paritok's own dashboard number for a coding-agent session: it reports
> saving sixty-four percent of my input tokens.
>
> And this is what the provider actually billed for that same session: sixty-nine
> percent *more* than if I'd sent the file with no compression at all.
>
> Same session. Both numbers are real. I'm going to show you why."

---

**0:20–0:55 — The mechanism**

> *[Screen: scroll to the POST ledger, point at the two rows]*
>
> "Paritok compresses your context and forwards it. But when the model needs the
> exact original, it calls `expand_context` — and the proxy answers that itself,
> by appending the full file to the request and sending it upstream a second
> time.
>
> That's this row. Twenty-seven thousand tokens. The provider bills it.
>
> Paritok's stats are computed once, before that loop runs. So it never sees this
> POST. It reports the first one and stops counting."

---

**0:55–1:35 — Live demo**

> *[Screen: paste a file, click Run, let the stages tick]*
>
> "This is running two real Paritok proxies, right now — one stock, one patched.
> It drives an actual multi-turn session through each and counts every upstream
> POST. Compression is happening on Paritok's hosted 4B model.
>
> *[cut to finished result]*
>
> Stock Paritok: six POSTs, a hundred and four thousand tokens billed."

---

**1:35–2:15 — The fix**

> *[Screen: the comparison bars]*
>
> "The fix is one idea. When the model calls `expand_context`, it's telling you
> that compressing that content was a mistake. That's a free supervision signal
> and nothing uses it.
>
> So: pin it. Pass that content through verbatim from then on. The reference
> never comes back, so there's nothing left to expand.
>
> Four POSTs instead of six. Twenty-nine percent fewer billed tokens. Two fewer
> round-trips.
>
> And when the model never expands, the two are identical — pinning costs nothing
> when it never fires."

---

**2:15–2:45 — Honesty**

> *[Screen: the limitations section]*
>
> "Two things I want to be straight about.
>
> This doesn't make that session profitable. It goes from seventy-four percent
> overspend to twenty-three. The pin is learned from the first expansion, so the
> first turn is always paid in full.
>
> And I haven't modelled prompt caching, which would lower the absolute numbers —
> though not the direction."

---

**2:45–3:00 — Close**

> *[Screen: the repo]*
>
> "Everything's Apache 2.0, and every number reproduces from the repo — real
> proxies, the released package, the real model.
>
> Paritok built a dial for this. It just doesn't have a controller yet."

---

## Recording notes

- **Warm the app** by loading it once before recording.
- **Pre-run a session** so you can cut to results rather than filming a wait.
- Set the browser to **dark theme** — the reconciliation reads harder against it.
- Zoom the browser to ~125% so numbers are legible at video resolution.
- No music. The numbers do the work; a soundtrack makes it feel like an advert.
