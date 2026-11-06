-- The prompt registry (step 9): which registered prompt a request used, as name@version (prompts.py), so a usage
-- row, and any eval or cost question about it, points at the exact prompt text. '' for requests that didn't name one.
alter table usage add column prompt text not null default '';
