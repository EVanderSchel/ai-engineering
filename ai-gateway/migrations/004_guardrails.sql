-- Guardrails (step 5): a team's own policy, the judge's price, and what the guardrails did, per request.

-- A team's guardrail settings, layered over each route's: each setting ends up at the stricter of the two, so a team
-- can tighten its routes' guardrails but never loosen them. '{}': the routes' settings as they are.
alter table teams add column guardrails jsonb not null default '{}';

-- The injection judge (Claude Haiku 5.5 on the Claude API, prompts up to 100K tokens). Its cost is charged to the
-- team whose request it screened.
insert into prices (provider, model, input, output, cache_read, cache_write, effective_from) values
    ('anthropic', 'claude-haiku-5-5', 0.10, 0.50, 0.01, 0.125, '2026-01-01');

-- Per request: what the guardrails found and did (check, action and kinds or rules; never text or values), and
-- what the judge cost. A request the guardrails refused has outcome 'blocked' and no provider.
alter table usage
    add column guardrails text not null default '',
    add column screen_cost_usd numeric(14, 6) not null default 0;
