# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
OpenAI Codex.

## What I used them for
Repository inspection, implementation drafts, focused test generation, running the test suite,
security/code review, and checking the generated small-dataset reports.

## What I wrote or decided myself
I supplied the incremental requirements and reviewed the design at each boundary: normalization,
PII protection, SQLite identity, lead merging, event reconciliation, and report behavior. I chose
the final scope and required purpose-separated lookup keys, transactional file markers, sanitized
event storage, and exact contract tests.

## Where the AI was wrong or unhelpful
The initial lookup design reused the Fernet key material directly for HMAC. I required a separate
purpose-derived HMAC key (`phone-lookup-v1`) after noticing that this coupled encryption and lookup
uses. Review also found that existing key-file permissions needed hardening and that rollback
tests did not initially exercise counters and rejection rows strongly enough; both were fixed.

## How I verified AI-generated code
I reviewed the code and diffs, ran focused tests after each slice, ran the full pytest suite, and
executed the documented command against both generated drops. I inspected all five output files,
checked row counts and data-quality totals, and reran the pipeline to confirm byte-identical
outputs.
