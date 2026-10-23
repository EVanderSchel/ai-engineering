import { HashRouter, Route, Routes } from 'react-router';
import { type Api, ApiContext } from './api/client';
import { ApplicationPage } from './pages/ApplicationPage';
import { QueuePage } from './pages/QueuePage';

// Hash routes (#/applications/gp-039): the app is static files in a bucket (stage 5), and the part after # never
// reaches the server, so every route works without rewrite rules.
export function App({ api }: { api: Api }) {
  return (
    <ApiContext value={api}>
      <HashRouter>
        <div className="banner" role="note">
          Prairie Commons Fund: grant review{import.meta.env.VITE_API_URL ? '' : ' (local fixtures)'}
        </div>
        <Routes>
          <Route path="/" element={<QueuePage />} />
          <Route path="/applications/:applicationId" element={<ApplicationPage />} />
          <Route path="*" element={<main><p>Page not found.</p></main>} />
        </Routes>
      </HashRouter>
    </ApiContext>
  );
}
