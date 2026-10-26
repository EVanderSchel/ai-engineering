import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { defaultApi } from './api/client';
import { defaultAuth } from './auth';
import './styles.css';

const auth = defaultAuth(); // undefined without a user pool configured: fixtures, or the local API's fixed reviewer

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App api={defaultApi(auth)} auth={auth} />
  </StrictMode>,
);
