# Agent loop — knowledge in NEDB, retrieval by cast

An agent whose memory is a NEDB database and whose retrieval tool is a
**3.3M-parameter model** running inside the daemon.

```bash
nedbd --dag --cast ./data                    # needs --features cast
python3 agent.py --seed --demo
```

```
[1] memories with importance 5
    tool: recall
    nql  FROM memories WHERE importance >= 5
    3 rows
      · release flow is: tag locally, then release.py --target github
      · never retag a release, always bump the version
      · guardrail: never push to interchained org without asking

[5] runs by vex
    tool: recall
    nql  FROM runs WHERE owner = "vex"
    3 rows
      · {"status": "done", "turns": 6, "misses": 0, ...}
      · {"status": "running", "turns": 6, ...}      ← this session, mid-flight
```

---

## Cast is a tool, not the agent

This distinction is the entire design. Ask the model to plan:

```
"what should I do next"
  →  FROM FROM FROM FROM AND AND AND AND = = = ...
```

It has **one output type**: NQL. It cannot choose a tool, write prose, or decide
anything. What it can do is turn *"memories with importance 5"* into
`FROM memories WHERE importance >= 5` in a few milliseconds, on CPU, for free.

So the loop splits three ways:

| job | who does it |
|---|---|
| pick a tool | the caller (keyword routing here, an LLM in production) |
| English → query | **cast** |
| run the query | NEDB |

A 3.3M-param model that reliably writes one clause type beats a 70B model doing
it slower and over the network — as long as you never ask it for anything else.

## Why the knowledge lives in NEDB

Three things a vector store does not give you:

- **`caused_by`** — every memory records the run that produced it.
  `TRACE caused_by` answers *"why do I believe this"* with a chain, not a
  cosine score.
- **`AS OF`** — replay what the agent knew at any past sequence. Debug a
  decision against the knowledge it actually had.
- **`verify()`** — hash-chained. Nobody edits a memory after the fact and
  pretends it was always there.

## Iteration

Each turn appends to `runs`; memories written that turn point back at it. Turn N
can query turns 1..N-1 through the same interface it uses for everything else.
There is no separate conversation-memory system — it is all NEDB.

Turn 5 above is the proof: the agent queried its own currently-executing run.

## The failure mode that matters

`SEARCH` terms outside the 581-token vocabulary get **silently substituted**:

```
"memories about pricing"  →  FROM memories SEARCH "handoff"
```

Measured on this checkpoint:

| term | in vocab | result |
|---|---|---|
| `release flow` · `guardrail` · `handoff` | yes | **3/3 copied** |
| `pricing` · `deadlines` · `kubernetes` | no | **0/3** — all became `"handoff"` |

That query parses. It names a real collection. It returns real rows. `valid:
true` and `collection_known: true` are both satisfied — and it answers a
question nobody asked. **Worse than an error, because it looks like an answer.**

**The engine catches it** (nedbd >= 2.8.2). `cast_checked` compares every quoted
literal in the plan against the prompt; one that isn't there was substituted, and
the response carries a `drift` field saying so.

That check started here, in this example. It belongs in the daemon — every
client inherits it instead of each one reimplementing it, exactly like the
collection check. A safety property that production callers need does not belong
in an examples directory.

Advisory, never fatal: the plan may still be right. But an unattended caller
should gate on all three —

```python
if plan["valid"] and plan["collection_known"] and not plan.get("drift"):
    rows = query(plan["nql"])
```

```
[6] memories about pricing
    nql  FROM memories SEARCH "handoff"
    drift generated the literal "handoff", which does not appear in the prompt
    → rephrase using words the model knows, or use `query` with explicit NQL
```

Same root cause as truncated digits (`height 400000 → 4000`): no copy mechanism
over prompt tokens. It's the first thing a v2 should fix.

## Schema

Field names come from the model's `agent` training domain, so they're in its
vocabulary:

```
memories     importance(1-5)  category(preference|project|people|domain)  agent
runs         status(running|done|failed|killed)  owner  duration  started_at
checkpoints  step  loss  params
relations    produced_by  recalled_by  cites
```

Invent your own field names and the model still writes queries against these.
It is a 3.3M-parameter model, not a schema reader.

## Prompts that work

```bash
python3 agent.py "memories with importance 5"
python3 agent.py "memories grouped by category count"
python3 agent.py "runs that failed"
python3 agent.py "remember cast defaults execute to false"
python3 agent.py 'FROM memories WHERE importance >= 4 LIMIT 3'   # explicit NQL
```

`TRACE` (96.5%) and `TRAVERSE` (93.3%) are its strongest clauses. `GROUP BY`
(77.0%) is its weakest. Name the field when a number could be a limit —
*"memories with importance over 3"*, not *"memories over 3"*.
