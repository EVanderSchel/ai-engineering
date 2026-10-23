// How the UI gets its data: one interface, two sources. Fixtures (the dev packets as the Python API returns them,
// tests/test_ui_contract.py) while building locally; an API once VITE_API_URL is set: the local one
// (tests/local_api.py, `npm run dev:api`) or the deployed one (stage 5).
import { createContext, useContext } from 'react';
import type { ApplicationRecord, DecisionRequest, Evidence, Queue } from './types';

export type Status = ApplicationRecord['status'];
export const STATUSES: Status[] = ['waiting_for_review', 'decided', 'declined_by_rules'];

export interface Api {
  queue(status: Status): Promise<Queue>;
  application(applicationId: string): Promise<Evidence>;
  decide(applicationId: string, request: DecisionRequest): Promise<Evidence>;
}

/** The API refused or failed: its status code and its own explanation ({"error": ...}). */
export class RequestFailed extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export class NotFound extends RequestFailed {
  constructor(message: string) {
    super(404, message);
  }
}

export function httpApi(baseUrl: string, fetchImpl: typeof fetch = fetch.bind(globalThis)): Api {
  async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetchImpl(new URL(path, baseUrl), {
      ...init,
      headers: { accept: 'application/json', ...(init.body ? { 'content-type': 'application/json' } : {}) },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const message = typeof body.error === 'string' ? body.error : `HTTP ${response.status}`;
      throw response.status === 404 ? new NotFound(message) : new RequestFailed(response.status, message);
    }
    return (await response.json()) as T;
  }
  return {
    queue: (status) => call(`applications?status=${encodeURIComponent(status)}`),
    application: (applicationId) => call(`applications/${encodeURIComponent(applicationId)}`),
    decide: (applicationId, request) =>
      call(`applications/${encodeURIComponent(applicationId)}/decision`, {
        method: 'POST',
        body: JSON.stringify(request),
      }),
  };
}

// Loaded on demand, one file per request, as the real API would answer.
const queues = import.meta.glob<Queue>('../fixtures/queue/*.json', { import: 'default' });
const applications = import.meta.glob<Evidence>('../fixtures/applications/*.json', { import: 'default' });

export function fixtureApi(): Api {
  async function load<T>(files: Record<string, () => Promise<T>>, path: string): Promise<T> {
    const loader = files[path];
    if (!loader) throw new NotFound(path);
    return loader();
  }
  return {
    queue: (status) => load(queues, `../fixtures/queue/${status}.json`),
    application: (applicationId) => load(applications, `../fixtures/applications/${applicationId}.json`),
    decide: () =>
      Promise.reject(new RequestFailed(501, 'Fixtures are read-only: decisions need the local API (npm run dev:api).')),
  };
}

export function defaultApi(): Api {
  const url = import.meta.env.VITE_API_URL;
  if (!url) return fixtureApi();
  // Relative ("/api/", proxied to the local API by the dev server) or absolute; always ending in "/".
  return httpApi(new URL(url.endsWith('/') ? url : `${url}/`, window.location.href).href);
}

// No default source: App always provides one. (A default of fixtureApi() would ship the fixtures in every build;
// without it, a build with VITE_API_URL set leaves them out.)
const missing = () => Promise.reject(new Error('no Api provided: render inside <App api={...}>'));
export const ApiContext = createContext<Api>({ queue: missing, application: missing, decide: missing });
export const useApi = () => useContext(ApiContext);
