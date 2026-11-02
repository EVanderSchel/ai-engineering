-- Self-hosted models (step 6): Qwen2.5-0.5B-Instruct on llama-server, one row per quantization (the model name the
-- server reports). Priced at 0 per token: no provider bills for them. Their real cost is the machine they run on,
-- which step 6 measures as cost per 1,000 requests at the measured throughput, rather than charging it per token.
insert into prices (provider, model, input, output, cache_read, cache_write, effective_from) values
    ('local', 'qwen2.5-0.5b-instruct-q4_k_m', 0, 0, 0, 0, '2026-01-01'),
    ('local', 'qwen2.5-0.5b-instruct-q8_0',   0, 0, 0, 0, '2026-01-01'),
    ('local', 'qwen2.5-0.5b-instruct-fp16',   0, 0, 0, 0, '2026-01-01');
