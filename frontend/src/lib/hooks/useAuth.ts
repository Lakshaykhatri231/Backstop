import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as authApi from "../api/auth";
import { clearToken, getToken, setToken } from "../auth-token";

export const authMeKey = ["auth", "me"] as const;

export function useCurrentCustomer() {
  return useQuery({
    queryKey: authMeKey,
    queryFn: authApi.me,
    enabled: !!getToken(),
    retry: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ email, password }: { email: string; password: string }) => authApi.login(email, password),
    onSuccess: ({ token, customer }) => {
      setToken(token);
      queryClient.setQueryData(authMeKey, customer);
    },
  });
}

export function useRegister() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ email, password, name }: { email: string; password: string; name: string }) =>
      authApi.register(email, password, name),
    onSuccess: ({ token, customer }) => {
      setToken(token);
      queryClient.setQueryData(authMeKey, customer);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return () => {
    clearToken();
    // removeQueries doesn't reliably clear an ACTIVE observer's `data` back
    // to undefined (a known TanStack Query gotcha — the mounted useQuery in
    // useCurrentCustomer keeps showing the last-fetched customer until
    // something re-renders it, which previously only happened on a full
    // page reload). resetQueries is the correct call for "instantly clear
    // this active query's data right now."
    queryClient.resetQueries({ queryKey: authMeKey });
    queryClient.removeQueries({ queryKey: ["cart"] });
    queryClient.removeQueries({ queryKey: ["catalog"] });
  };
}
