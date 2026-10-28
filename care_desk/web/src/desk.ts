import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { api, loadDesk } from './api';
import { useToast } from './ui';

// While a window or drawer is open, the 20-second refresh waits, so what
// someone is typing or ticking is never redrawn under them.
let holds = 0;
export const holdRefresh = () => {
  holds++;
  return () => { holds--; };
};

export function useDesk() {
  return useQuery({
    queryKey: ['desk'],
    queryFn: loadDesk,
    refetchInterval: () => (holds || document.visibilityState !== 'visible' ? false : 20_000),
    refetchOnWindowFocus: true,
    retry: 1,
  });
}

/** Post a change, say how it went, then reload the desk. Resolves to the reply, or null on failure. */
export function useAction() {
  const client = useQueryClient();
  const toast = useToast();
  return useCallback(async <T = Record<string, unknown>>(path: string, body: unknown = {},
    ok?: string | ((r: T) => string)): Promise<T | null> => {
    try {
      const r = await api<T>(path, body);
      if (ok) toast(typeof ok === 'function' ? ok(r) : ok);
      await client.invalidateQueries({ queryKey: ['desk'] });
      return r;
    } catch (e) {
      toast((e as Error).message, 'u');
      return null;
    }
  }, [client, toast]);
}
