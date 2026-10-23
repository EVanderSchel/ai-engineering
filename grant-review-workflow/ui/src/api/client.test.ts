import { NotFound, fixtureApi, httpApi } from './client';

function fakeFetch(status: number, body: unknown = {}) {
  const calls: string[] = [];
  const impl = (async (url: URL) => {
    calls.push(url.toString());
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return { impl, calls };
}

test('the HTTP API builds URLs under its base and encodes the ID', async () => {
  const { impl, calls } = fakeFetch(200, { applications: [] });
  const api = httpApi('https://api.example.test/prod/', impl);
  await api.queue('decided');
  await api.application('gp-039/../x');
  expect(calls).toEqual([
    'https://api.example.test/prod/applications?status=decided',
    'https://api.example.test/prod/applications/gp-039%2F..%2Fx',
  ]);
});

test('404 is NotFound, other failures are errors', async () => {
  await expect(httpApi('https://a.test/', fakeFetch(404).impl).application('x')).rejects.toBeInstanceOf(NotFound);
  await expect(httpApi('https://a.test/', fakeFetch(500).impl).application('x')).rejects.toThrow('HTTP 500');
});

test('the fixtures answer like the API, and an unknown ID is NotFound', async () => {
  const api = fixtureApi();
  const queue = await api.queue('waiting_for_review');
  expect(queue.applications.length).toBeGreaterThan(0);
  expect((await api.application('gp-039')).record.application_id).toBe('gp-039');
  await expect(api.application('nope')).rejects.toBeInstanceOf(NotFound);
});
