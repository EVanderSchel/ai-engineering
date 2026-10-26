import { useEffect, useState } from 'react';

export type Load<T> = { state: 'loading' } | { state: 'error'; error: Error } | { state: 'done'; value: T };

// Runs `load` whenever a key changes and keeps the latest answer only: a slow response for a page the reviewer
// has already left never overwrites the page they're on.
export function useLoad<T>(load: () => Promise<T>, keys: unknown[]): Load<T> {
  const [result, setResult] = useState<Load<T>>({ state: 'loading' });
  useEffect(() => {
    let current = true;
    setResult({ state: 'loading' });
    load().then(
      (value) => current && setResult({ state: 'done', value }),
      (error: Error) => current && setResult({ state: 'error', error }),
    );
    return () => {
      current = false;
    };
  }, keys); // the caller lists what the load depends on
  return result;
}
