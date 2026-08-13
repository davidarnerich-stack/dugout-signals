# End-to-end upload tests (DS-106)

    python3 -m pytest tests/ -q

## Why these exist

Every upload bug that reached production — DS-105, DS-107, DS-108, DS-114 —
lived between the HTTP request and the database. Reading the code missed them.
Querying the data missed them. They only appeared when a real GameChanger file
went through the real endpoint into the real database, which is exactly what
these tests do.

## Setup (one time)

1. `cp .env.example .env` and fill in the two secrets from Render.
2. A team named **ZZ Test Harness** must exist, owned by the
   `davidarnerich+harness@gmail.com` auth user. It already does. Tests skip
   with a clear message if it is missing rather than inventing one.

## What is real and what is faked

Real: routing, auth-required plumbing, every parser, every database write,
every constraint, and the coach's own export files.

Faked, and only these two:

- **Auth token validation** — `@auth_required` checks Supabase tokens on every
  request. That is DS-116's subject, not this suite's.
- **The model** — report generation makes several Anthropic calls per upload.
  Stubbed, so runs are free and fast. Report *content* is verified by reading
  real reports; what this suite protects is the write path beneath it.

## Safety

Every write is scoped to the test team, and `clean_team` empties it before and
after each test. `test_harness_writes_only_to_the_test_team` asserts the
scoping holds. If that test ever fails, stop running the suite against the
live database.

The test team belongs to its own coach deliberately. Attaching it to a real
coach would be unsafe: `_sync_team_session` picks a coach's team with an
unordered `limit(1)`, so a coach with two teams could be logged into either.

## Adding a test

Add it when a bug reaches production, and name the ticket in the docstring.
Then prove it works the way this suite was proved: reintroduce the bug, watch
the test fail, restore the fix, watch it pass. A test that has never failed has
not been shown to test anything.
