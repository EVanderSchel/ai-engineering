# grant-review-workflow reviewer UI (React, TypeScript)

Step 8: the page a reviewer uses. A queue of applications by status, and one page per application with the evidence
to decide it: flags, the fund's rules, figures from the return beside the public record and the narrative's claims,
the proposal as read, and the narrative itself. Decisions (stage 3) and sign-in (stage 4) come next.

```powershell
cd grant-review-workflow/ui
npm ci
npm run dev       # http://localhost:5173, on local fixtures
npm test          # vitest + Testing Library
npm run build     # type-check, then a production build in dist/
```

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
