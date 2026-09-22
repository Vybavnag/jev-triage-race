# Jev vs LLM: triage race
Created by Claude code

Race a **classifier** against a **language model** on the same job, watch
speed and cost diverge in real time, and judge the answers yourself.

Both sides get the same support ticket and the same three questions — is it
urgent, which team owns it, how frustrated is the customer. The bundled
tickets carry hand-written expected answers, shown beside each side's answer;
nothing in the app decides who was right. One side is
[Jev](https://docs.typesafe.ai/introduction), TypeSafe AI's System 1 model,
which returns typed probabilities. The other is an LLM answering with
structured output.

You can also paste your own tickets into the race, or hand both sides a block
of code and a short checklist of yes/no questions to review it against.

<p align="center">
  <img src="docs/system-one-vs-system-two.svg" width="820"
       alt="The same ticket goes to both sides. Jev evaluates three typed questions in parallel and returns a probability for each. The LLM generates one structured object asserting a single answer per field.">
</p>

## Quickstart

There is nothing to configure. The page asks for your
[TypeSafe key](https://console.typesafe.ai/keys) and your
[Anthropic key](https://console.anthropic.com/settings/keys) when it loads;
both sides then run on your own account.

```bash
git clone https://github.com/Vybavnag/jev-triage-race.git
cd jev-triage-race
```

Pick either way to run it. A `.env` is optional and only holds operator
settings (rate limits, a race token, an OpenAI-compatible endpoint); copy
`.env.example` if you want one.

### With Docker

```bash
docker build -t jev-race .
docker run --rm -p 8000:8000 jev-race        # add --env-file .env if you made one
```

Open <http://localhost:8000>. One container serves the API and the UI, which is
exactly what gets deployed.

### Without Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cd web && npm install && cd ..

./dev
```

Open <http://localhost:5173>. `./dev` runs the API and the UI together with hot
reload on both, and Ctrl-C stops both. Override the ports with `API_PORT` or
`UI_PORT` if something else is using them.

Prefer a single process? Build the UI once and the API will serve it:

```bash
cd web && npm run build && cd ..
.venv/bin/uvicorn app.main:app        # everything on http://localhost:8000
```

## What a run looks like

15 tickets from the bundled set, Jev against Claude Sonnet 5, run from a laptop
on 2026-09-21:

| | Jev | Claude Sonnet 5 |
|---|---|---|
| Average latency per ticket | 313 ms | 2,054 ms |
| Cost for the run | $0.000313 | $0.027396 |

About 6.6× the speed and 88× the cost difference. Against Claude Haiku 4.5 the
speed gap narrows to roughly 2× and the cost gap to about 23×. How the answers
compare is left to you: the results table puts each side's answer next to the
ticket's expected labels.

Treat these as one sample, not a benchmark: 15 synthetic tickets, one machine,
one moment. Latency includes network round trips from wherever you run it. The
point of the repo is that you can rerun it on your own data in a minute.

## What you are looking at

**Race** runs the bundled dataset through both sides at once. Each side has its
own concurrency budget so a slow LLM never throttles Jev, and the two tracks
fill independently — the gap between them is the whole point. Failed calls
are counted separately and kept out of the latency average, so a rate limit
never flatters a side.

Under the tracks, a **results table** fills in as answers arrive: one row per
ticket, with the ticket's expected answer from its label, then each side's
answer with its time. Jev's answers carry the probability behind them; the
LLM's carry nothing but the value. Nothing is marked right or wrong. The only
verdicts the app gives are the ones it can measure: which side was faster and
which was cheaper.

**Race your own tickets** by switching the source to "Paste your own" and
dropping in support messages, one per paragraph. They run through the same
pipeline with the same caps. They have no expected answers, so the table shows
both sides' answers and times and you compare them.

**Code review** is where the difference in *kind* shows up. Jev cannot write a
review, so a review here is a checklist: paste one block of code and a short
list of plain yes/no questions ("Does the code contain a hardcoded secret?",
"Is there repeated logic that could be a function?"), and both sides answer
every question about that code. It runs as a two-lane race like the tickets
do: each side's lane fills the moment its answer lands, so you watch Jev
finish while the LLM is still thinking. Jev returns a probability per
question, drawn as a bar; the LLM returns a bare yes or no through structured
output. Rows where the two sides disagree are marked. Nothing is graded — the
table is the comparison. The questions go into the prompt on both sides, never into the
LLM's output schema, so a new wording never changes what the LLM is allowed to
return.

## How the comparison is kept fair

Benchmarks are easy to rig, so here is every thumb that could have been on the
scale:

- **One request per message, per side.** No retry loops, no multi-step
  prompting, no self-consistency sampling on either side.
- **The LLM gets structured output**, not free text that would need parsing —
  its best shot at this task.
- **Claude runs at `effort: "low"`** with thinking left enabled. That is the
  setting a real classification workload would use. Disabling thinking is a
  known failure mode on Opus 5 and is deliberately avoided.
- **Automatic model fallbacks are off.** A refusal is recorded as an error, not
  silently answered by a different model. A reply that runs out of room or
  fails to parse is recorded as `malformed`, never as a guess.
- **Code review gives the LLM room.** Reviews run with `max_tokens` of 4096 so
  a long block of code with thinking on is never cut off mid-answer; triage
  keeps 512.
- **Cost uses published per-token prices** — Jev at $0.042 per million input
  tokens with output free, each Claude model at its own rate. If a provider
  does not report usage, cost reads `unknown` rather than `$0`.
- **The dataset is synthetic and hand-labeled.** It is a smoke test, not a
  benchmark to cite. Swap in your own labeled tickets for a number that means
  something to you.

One asymmetry is real and worth naming: Jev answers all three questions in a
single parallel evaluation, which is what the model is built for. That is not a
handicap applied to the LLM — it is the architectural difference being measured.

## Reading the results

| Question | Jev returns | Shown as |
|---|---|---|
| Urgent | probability in `[0, 1]` | yes when `p >= 0.5`, with the probability |
| Team | a pick plus a probability per option | the pick, with its probability |
| Frustration | weighted position on a 0-indexed scale | a level 1–5 |

Jev's score is a probability-weighted float over zero-indexed levels, so a
five-level rubric returns `0.0`–`4.0`. The app maps that to 1–5 with
`floor(score + 0.5) + 1`, deliberately not Python's `round()`, which rounds
half-to-even and is non-monotonic at exactly `.5`. The same `0.5` bar turns a
code-review probability into a yes or a no.

The expected column comes straight from the bundled labels. The app never
counts matches: neighbouring frustration levels are genuinely arguable, and
whether a borderline ticket was "urgent" is a judgement you can make better
with the text in front of you than a threshold can.

## Your keys

Provider keys are never in the environment. The page asks for a TypeSafe key
and an Anthropic key once per tab, keeps them in memory and `sessionStorage`
(never `localStorage`, so closing the tab forgets them), and sends them with
each run in the `X-TypeSafe-Key` and `X-Anthropic-Key` headers. The server
builds that run's provider clients from them, closes the clients when the run
ends, and keeps nothing: the keys are not stored, not logged, and never echoed
into a response or an event stream. A rejected key shows up as
`key rejected` on that side, with a notice saying which provider said no.

The one operator-configured provider is the optional OpenAI-compatible
endpoint, because it carries a base URL the server will connect to; letting
visitors set that would let them point the server at anything.

## Configuration

Everything below is server-side and optional. The browser can only pick an
opponent from the allowlist the server publishes; it cannot set a URL or a
price.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_COMPAT_BASE_URL` / `_API_KEY` / `_MODEL` | — | Any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM). Operator-only. |
| `OPENAI_COMPAT_PRICE_IN` / `_OUT` | — | $/MTok for the cost counter. Unset means cost shows `unknown`. |
| `RACE_TOKEN` | unset | Gate race starts, cancels, and code reviews behind a bearer token. Holders are not rate-limited. |
| `RACE_MAX_ITEMS` | `25` | Hard server-side cap per race, bundled or pasted. |
| `RATE_LIMITING` | `true` | Set to `false` on a local clone to switch every per-IP limit off. |
| `RACE_BURST` / `RACE_PER_MINUTE` | `3` / `1` | Races each IP may start at once, then how many per minute. |
| `REVIEW_BURST` / `REVIEW_PER_MINUTE` | `5` / `2` | Same for code reviews. |
| `REVIEW_IN_FLIGHT` | `2` | Reviews the whole process runs at once; a third gets a 503. Applies even with a token. |
| `JEV_CONCURRENCY` / `LLM_CONCURRENCY` | `8` / `4` | Per-side parallelism. |
| `REVIEW_ATTEMPT_TIMEOUT` / `REVIEW_TIMEOUT` | `25` / `60` | Seconds per review attempt, and the outer bound per side. |
| `TRUST_PROXY` | `false` | Honor `X-Forwarded-For` for rate limiting. |
| `ENV` | `dev` | Set to `prod` to disable `/docs`. |

### Rate limiting

Visitors spend their own provider keys, so the limits exist to keep one
person from monopolising the server, not to protect a wallet. In public mode
(no `RACE_TOKEN`) each IP may start a burst of three races and then one a
minute, and a burst of five code reviews and then two a minute; only one race
runs at a time, and at most two reviews run at once across the whole process.
Requests refused for any other reason (a bad body, a missing key, an unknown
model) never spend a token, and a refused request gets a `429` with a
`Retry-After` header or a `503` when every review slot is taken.

To relax the limits, set the variables in the table above. To switch them off
on a machine only you use, set `RATE_LIMITING=false`. To remove them from the
code, everything lives in one place: `race_bucket`, `review_bucket`,
`review_slots`, `configure_limits`, and `consume_rate_limit` in `app/deps.py`,
called from `start_race` in `app/routers/race.py` and `start_review` in
`app/routers/review.py`. Delete those calls and the `review_slots.available`
check, and the tests that pin them (`test_review_rate_limit_bursts_five_then_429`,
`test_review_busy_when_two_reviews_are_in_flight`, and neighbours in
`tests/integration/test_api.py`) will tell you what else to drop.

### Before you expose it publicly

Every run uses the keys the visitor typed, so nobody can spend your TypeSafe
or Anthropic credits. The one exception is an OpenAI-compatible endpoint, if
you configured one: that opponent runs on your key for every visitor, so on a
public instance leave it unset, keep the per-IP limits tight, or make the
instance private with `RACE_TOKEN`. If you put it on the internet:

1. Keep `RATE_LIMITING` on, and size the limits for the traffic you expect.
2. Serve it over HTTPS. The visitor's keys travel in request headers, so the
   platform's TLS is what keeps them private on the wire.
3. Set `ENV=prod` to close the docs endpoints.
4. Set `RACE_TOKEN` only for a private instance: holders skip the per-IP
   limits, so treat the token like an API key.

Pasted tickets and code are sent to TypeSafe and to the visitor's LLM
provider, and the page says so; the server never keeps them either.

## Deploying

The repo is application code only — no Terraform, no Helm, no CI config. It
builds to one container that serves both the API and the frontend, so any
platform that can run a Dockerfile can run it. The only thing this repo needs
to know about infrastructure is which port to listen on.

## Tests

```bash
pytest                       # 300 tests, coverage gate at 70%
cd web && npm test           # key store, ticket splitting, question parsing, stream reducers, design guards
cd web && npm run build      # tsc --noEmit, the production build, then scripts/check-dist.mjs
```

The suite never touches a real API: provider adapters run against injected
fakes and `respx`-mocked transports. Notable cases include the score-mapping
boundaries, the latency average excluding errors, per-side concurrency caps, cancellation actually cancelling in-flight calls,
`Last-Event-ID` replay, a truncated or refused LLM reply landing as a verdict
rather than a crash, every review rejection (oversized code, duplicate or
invisible-character questions, a lone surrogate) happening before any provider
is called, a missing or malformed visitor key being refused before any
provider or bucket is touched, per-run provider clients being closed however
the run ends, the two-reviews-in-flight cap, the limits being sized from the
environment and switchable off, and a sweep asserting neither visitor key and
no pasted text appears in any response body or SSE frame.

## How it is built

<p align="center">
  <img src="docs/architecture.svg" width="820"
       alt="The browser holds no API keys: it posts a race request and receives results over Server-Sent Events, while the server runs two independently rate-limited lanes that call the Jev and LLM APIs.">
</p>

- `app/scoring.py`, `app/pricing.py`, `app/review.py` — pure functions, no I/O: label mapping, latency and cost tallies, prices, the review checklist
- `app/classifiers/` — one adapter per provider; each has one private call
  core and two thin public methods, `classify` for triage and `review` for code
- `app/race.py` — two `asyncio.TaskGroup`s, per-side semaphores, byte-bounded event buffer
- `app/deps.py` — visitor keys read from headers, one `Providers` per run, and every rate limit
- `app/routers/` — REST, the SSE streams, and the review endpoint
- `web/` — React and TypeScript, no state library; keys live in `web/src/keys.ts`

The fonts (Poppins, Manrope, JetBrains Mono) are self-hosted: `web/src/main.tsx`
imports the latin subsets from the `@fontsource` packages, so Vite serves
them from this origin and the `default-src 'self'` policy never needs a font
host. `npm run build` ends with `web/scripts/check-dist.mjs`, which fails the
build if the bundle ever references another origin or a `data:` URI, or if
the fonts are missing; `web/src/design.test.ts` pins the other design rules
(solid fills, 16px form controls so phones do not zoom, no fixed chrome, the
secret inputs' autofill opt-outs, no `localStorage`).

Server-Sent Events carry the race rather than WebSockets: the traffic is
one-directional, `EventSource` reconnects on its own, and a plain HTTP response
passes through platform proxies without upgrade handling.

Race state lives in memory. Restarting the process loses history, which is
fine for a demo and deliberate — there is no database to run.