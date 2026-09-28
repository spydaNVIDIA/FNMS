# Assignment Journal — Lab1A

I used the principles of security, speed, and reliability to design this implementation.

**Stack** FastAPI was the simplest backend choice for this project while still providing powerful syntactic sugar. FastAPI's Pydantic models facilitated an easy security win: my response schema only contains `id`, `username`, `email`, and `created_at`, which makes leaking a password hash structurally impossible rather than a thing I have to remember not to do (API Rule 1). For the frontend I chose static HTML + Tailwind (CDN) + vanilla `fetch` to avoid a build step and keep things simple. Since the frontend is served on port 5173 while the backend runs on 8000, every request is cross-origin.

**Database** I ran Postgres in Docker with a named volume. Structured, relational
data was the obvious fit for users with a unique username constraint, and the
named volume facilitates persistence of the data — the container is disposable but the volume is not. I chose this implementation for simplicity rather than using a hosted DB like Neon: bring-up and teardown is simple with Docker.

**Auth** Passwords are hashed with argon2id (the OWASP-recommended algorithm) via `argon2-cffi`, and sessions are stateless JWTs signed with HS256. The main tradeoff of stateless tokens is that they can't be revoked before expiry, so I kept the lifetime short. Bad, missing, or expired tokens all funnel to a single 401 path (API Rule 2). For API Rule 3 I chose returning **404** over 403 when a user touches an id that isn't theirs: returning 403 would confirm that another account exists, so 404 leaks less and defends against enumeration. I carried the JWT as a Bearer header in localStorage rather than an HttpOnly cookie, which sidesteps SameSite and CSRF concerns. The risk with this approach is Cross-Site Scripting: injected scripts can read localStorage and steal the token.

**Challenges & learnings** The most educational part was CORS. Seeing a preflight
`OPTIONS` fail until I registered the exact frontend origin in the `.env` file made the "different origins" point concrete rather than abstract. For example, if I only allow-listed `http://127.0.0.1:5173` but the user opened the frontend at `http://localhost:5173`, the browser would block the response from the server, since it treats those two URLs as different origins. I also enjoyed how much correctness Pydantic buys you: validation, status codes, and response shaping all become declarative, which allowed for a concise implementation.
