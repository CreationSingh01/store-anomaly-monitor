# Architecture Decision Record — Store Anomaly Monitor

---

## 1. LangGraph over a simple sequential script

**Context**  
The pipeline has three discrete stages — detect, diagnose, communicate — each
with its own failure mode.  A flat script (`monitor(); analyst(); communicator()`)
would work for the happy path but couples error handling, state passing, and
routing logic into one block of procedural code.

**Decision**  
Use LangGraph's `StateGraph` to model the pipeline.  Each agent is a node;
shared data travels through a `TypedDict` state object; conditional routing
after the monitor node handles the zero-anomaly short-circuit.

**Consequences**  
Each node is isolated — an exception in the analyst node does not prevent the
run log from being written, and the communicator node never sees partial
diagnoses.  Adding a new stage (e.g. a Slack notifier) means adding one node
and one edge, not restructuring procedural code.  The tradeoff is a LangGraph
dependency and a small amount of boilerplate for a pipeline that is currently
linear; the abstraction pays off most if branching or retries are added later.

---

## 2. Same-period MTD comparison instead of full previous month

**Context**  
MTD metrics are inherently biased by how many days have elapsed.  Comparing
"April 1–17 sales" against "all of March sales" would make every April store
look like it is underperforming simply because fewer days have passed.

**Decision**  
Compare current MTD (April 1–17) against the exact same day-count window of
the prior month (March 1–17).  The `_period_bounds()` helper in monitor_agent
derives the prior period ceiling as `min(ref.day, days_in_prev_month)` to
handle short months correctly.

**Consequences**  
Deviation percentages reflect genuine performance differences rather than
elapsed-time artefacts.  The approach is intuitive to business users ("same
17 days last month") and stable — adding a week's data will not flip stores
in and out of alert status.  The limitation is that it ignores full-month
seasonality patterns (e.g. end-of-month spikes), which the data generator
partially accounts for but the comparison does not explicitly normalise.

---

## 3. tool_use for Claude API instead of free-text parsing

**Context**  
The analyst agent needs Claude to return a structured record with specific
fields (diagnosis, severity, recommended_action).  Parsing these from a
free-text response requires fragile string matching or JSON extraction from
markdown code blocks, both of which break when the model changes its
phrasing.

**Decision**  
Define a `submit_diagnosis` tool with a strict JSON Schema and use
`tool_choice={"type": "tool", "name": "submit_diagnosis"}` to force Claude
to call it.  The response `block.input` is already a validated Python dict.

**Consequences**  
Structured output is guaranteed by the API contract, not by post-processing.
The field list is enforced at the schema level; a missing `severity` field
causes a model error rather than a silent None downstream.  The cost is one
additional token overhead for the tool definition, and the approach requires
the Anthropic SDK rather than a generic HTTP client.

---

## 4. One email per store instead of one per anomaly

**Context**  
A store with three flagged metrics would generate three separate emails if
each anomaly triggered independently.  A regional manager receiving 11 emails
for four stores in one morning would experience the system as noise rather
than signal.

**Decision**  
The communicator agent groups anomalies by `store_id` before composing
emails.  Each store receives exactly one email regardless of how many metrics
are flagged, with a summary table listing all anomalies and a diagnosis card
per metric.

**Consequences**  
Email volume scales with the number of affected stores, not the number of
flags — typically 1–5 emails per run rather than 10–20.  The store-level
grouping also makes it natural to include cross-metric context (e.g. both
walk-in drop and ABV drop appear in the same email, making a systemic issue
obvious).  The limitation is that severity routing (e.g. "page on-call for
critical only") must be implemented per store rather than per anomaly.

---

## 5. Synthetic data with seeded anomalies instead of random noise

**Context**  
Real retail data is unavailable during development and testing.  A purely
random dataset would make it impossible to verify that the detection logic
is correct, because any "anomalies" found would be artefacts of the random
seed rather than signal.

**Decision**  
Generate six months of realistic data with deterministic seasonality
(December peak, January trough, weekend uplift, month-end spike), then
inject known anomalies into exactly four stores for April 2025.  Each
seeded store has a distinct pattern: low KRA, ABV drop with normal footfall,
walk-in collapse, and all-metrics failure.

**Consequences**  
The monitor agent can be validated by confirming it flags exactly those four
stores and no others.  The anomaly types are differentiated enough to test
the analyst agent's ability to produce distinct diagnoses per pattern.  The
limitation is that the synthetic seasonality is a simplified approximation;
behaviour on genuinely noisy real data may differ.

---

## 6. Mock fallback mode for both Claude API and Gmail

**Context**  
The pipeline has two external paid services with credentials that may not be
available in all environments: the Anthropic API and Gmail SMTP.  Blocking
development or CI on live credentials would slow iteration and risk accidental
charges or email sends during testing.

**Decision**  
Both analyst_agent and communicator_agent check for placeholder credential
values (`"add_later"`, empty string) at startup.  In mock mode the analyst
returns deterministic template diagnoses keyed on anomaly_type; the
communicator prints stripped HTML to stdout instead of calling SMTP.  The
mock path exercises the same code branches, Supabase writes, and run-log
entries as the live path.

**Consequences**  
The full pipeline can be developed, tested, and run in CI without any live
API keys.  Output is deterministic and predictable, making regressions easy
to spot.  The tradeoff is that mock diagnoses do not test the quality of
Claude's actual reasoning; a separate integration test with a live key is
needed to validate prompt behaviour.

---

## 7. Supabase for both data storage and audit logging

**Context**  
The project needs: (a) a queryable store for 3,000+ rows of daily sales data,
(b) a table for MTD summaries refreshed daily, (c) an alert ledger that
survives between runs, and (d) an audit trail of agent executions.  Each of
these could be served by a different system (S3 for CSVs, Redis for state,
SES for logs), but that would add four integration points to a project whose
complexity should stay low.

**Decision**  
Use a single Supabase project for all four concerns:
`store_daily_sales`, `store_mtd_summary`, `anomaly_alerts`, and
`agent_run_log`.  All agents connect via the same `supabase-py` client
initialised from two env vars.

**Consequences**  
There is one credential pair, one connection pattern, one place to inspect
data, and one schema to version.  The PostgREST REST API used by `supabase-py`
is sufficient for the access patterns here (range queries, upserts, status
updates).  The limitation is that Supabase's REST layer cannot run arbitrary
SQL without a custom RPC function, which is why the `ensure_table` logic in
`seed_supabase.py` degrades gracefully when `exec_sql` is absent.  For a
production system with complex reporting queries, a direct Postgres connection
via `psycopg2` would be added alongside the REST client.
