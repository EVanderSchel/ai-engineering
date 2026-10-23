import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { defaultApi } from './api/client';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App api={defaultApi()} />
  </StrictMode>,
);
