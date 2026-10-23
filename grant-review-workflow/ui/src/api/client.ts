// How the UI gets its data: one interface, two sources. Fixtures (the dev packets as the Python API returns them,
// tests/test_ui_contract.py) while building locally; the real API once VITE_API_URL is set (stage 5).
import { createContext, useContext } from 'react';
import type { ApplicationRecord, Evidence, Queue } from './types';

export type Status = ApplicationRecord['status'];
export const STATUSES: Status[] = ['waiting_for_review', 'decided', 'declined_by_rules'];

export interface Api {
  queue(status: Status): Promise<Queue>;
  application(applicationId: string): Promise<Evidence>;
}

export class NotFound extends Error {}

export function httpApi(baseUrl: string, fetchImpl: typeof fetch = fetch.bind(globalThis)): Api {
  async function get<T>(path: string): Promise<T> {
    const response = await fetchImpl(new URL(path, baseUrl), { headers: { accept: 'application/json' } });
    if (response.status === 404) throw new NotFound(path);
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    return (await response.json()) as T;
  }
  return {
    queue: (status) => get(`applications?status=${encodeURIComponent(status)}`),
    application: (applicationId) => get(`applications/${encodeURIComponent(applicationId)}`),
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
  };
}

export function defaultApi(): Api {
  const url = import.meta.env.VITE_API_URL;
  return url ? httpApi(url.endsWith('/') ? url : `${url}/`) : fixtureApi();
}

// No default source: App always provides one. (A default of fixtureApi() would ship the fixtures in every build;
// without it, a build with VITE_API_URL set leaves them out.)
const missing = () => Promise.reject(new Error('no Api provided: render inside <App api={...}>'));
export const ApiContext = createContext<Api>({ queue: missing, application: missing });
export const useApi = () => useContext(ApiContext);
