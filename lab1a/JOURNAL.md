# Assignment Journal — Lab1a

When I approached this as a systems problem instead of a coding exercise, the
"holy trinity" of security, speed, and reliability drove every decision.

**Stack.** My machine has Python but no Node, so FastAPI was the low-friction
backend choice — and the assignment itself links FastAPI's auth docs, so it's a
well-supported path. FastAPI's Pydantic models gave me a security win almost for
free: my response schema only contains `id`, `username`, `email`, and
`created_at`, which makes leaking a password hash structurally impossible rather
than a thing I have to remember not to do (Rule 1). For the frontend I chose
static HTML + Tailwind (CDN) + vanilla `fetch`. It has zero build step, which
keeps the whole project genuinely simple, and because it's served on port 5173
while the API runs on 8000, every request is truly cross-origin — exactly the
network behavior the course is about.

**Database.** I ran Postgres in Docker with a named volume. Structured, relational
data was the obvious fit for users with a unique username constraint, and the
named volume is what makes "data survives a restart" true — the container is
disposable but the volume is not. The tradeoff versus a hosted DB like Neon is
that a grader must have Docker, but in return there are no live cloud credentials
and the setup is fully reproducible with `docker compose up -d`.

**Auth.** I wrote auth myself to understand it. Passwords are hashed with
argon2id (the OWASP-recommended algorithm) via `argon2-cffi`, and sessions are
stateless JWTs signed with HS256. The main tradeoff of stateless tokens is that
they can't be revoked before expiry, so I kept the lifetime short. Bad, missing,
or expired tokens all funnel to a single 401 path (Rule 2). For Rule 3 I chose
**404** over 403 when a user touches an id that isn't theirs, applied
consistently to GET/PATCH/DELETE — returning 403 would confirm that another
account exists, so 404 leaks less and defends against enumeration.

**Challenges & learnings.** The most educational part was CORS. Seeing a preflight
`OPTIONS` fail until I registered the exact frontend origin made the "different
origins" point concrete rather than abstract. I also enjoyed how much correctness
Pydantic buys you: validation, status codes, and response shaping all become
declarative, which is what let the implementation stay small while still passing
the grader's shape and status-code checks. The biggest lesson was designing for
the person who runs your project cold — writing the README as literal,
copy-pasteable commands and then following it myself.
