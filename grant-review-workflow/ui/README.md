# grant-review-workflow reviewer UI (React, TypeScript)

Step 8: the page a reviewer uses. A queue of applications by status, and one page per application with the evidence
to decide it: flags, the fund's rules, figures from the return beside the public record and the narrative's claims,
the proposal as read, and the narrative itself. While an application waits, the reviewer approves or declines it with
a reason (quoted in the letter), or corrects figures read from the return, which sends it back through the rules.
Reviewers sign in with the Cognito user pool (stage 4).

```powershell
cd grant-review-workflow/ui
npm ci
npm run dev       # http://localhost:5173, on local fixtures (read-only)
npm run dev:api   # the same, on the local API: decisions work (start it first, below)
npm test          # vitest + Testing Library
npm run build     # type-check, then a production build in dist/
```

**The local API.** `uv run python tests/local_api.py` (from `grant-review-workflow/`) runs the real API handler
behind a stand-in for API Gateway on http://127.0.0.1:8000, signed in as a fixed local reviewer, with the dev packets
loaded and decisions resumed in-process by stand-in steps: no AWS, no model calls, all in memory. `npm run dev:api`
proxies `/api` to it.

**Sign-in.** With the user pool configured, nothing shows until a reviewer signs in on Cognito's hosted page
(`src/auth.ts`, oidc-client-ts: authorization code flow with PKCE, tokens in sessionStorage, refresh tokens rotated,
sign-out revokes the refresh token and ends the Cognito session). Every API request carries the access token; the
local API checks it as API Gateway's JWT authorizer will, and `src/api.py` checks the reviewers group. Configure it in
`ui/.env.api.local` (gitignored: this account's IDs, from the `GrantReviewAuth` stack's outputs), which both
`npm run dev:api` and the local API read:

```
VITE_COGNITO_USER_POOL_ID=us-east-2_...
VITE_COGNITO_CLIENT_ID=...
```

Without it, there's no sign-in: the fixtures, or the local API's fixed reviewer. The local API prints why it refuses a
token (never the token).

**Data.** `src/api/client.ts` has one interface with two sources. Without `VITE_API_URL` the app reads
`src/fixtures/`: the dev packets run through the workflow with stand-in steps (no model calls) and read back
through the real API handler (`src/api.py`). Their rubric quotes and letters are placeholders. With
`VITE_API_URL` set, it calls the API, and the build leaves the fixtures out.

**Types.** `src/api/types.ts` is generated from `src/api/schema.json`, which the Python API exports from its
Pydantic models. After changing the API's responses:

```powershell
cd grant-review-workflow
$env:UPDATE_UI_CONTRACT = "1"; uv run pytest tests/test_ui_contract.py; Remove-Item Env:UPDATE_UI_CONTRACT
cd ui; npm run types
```

`tests/test_ui_contract.py` (Python) fails when the schema or fixtures are stale, and `src/api/types.test.ts`
fails when the types are.

**Applicant text** (the form, the narrative, quotes from it) is rendered by React as text, never as HTML or markdown.
`src/App.test.tsx` checks that a narrative hiding an instruction in an HTML comment shows it as plain text.
