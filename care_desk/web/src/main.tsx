import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { Toasts } from './ui';
import './desk.css';
import './app.css';

const client = new QueryClient();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <Toasts>
        <App />
      </Toasts>
    </QueryClientProvider>
  </StrictMode>,
);
