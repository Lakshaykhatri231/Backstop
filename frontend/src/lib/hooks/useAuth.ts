import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as authApi from "../api/auth";
import { clearToken, getToken, setToken } from "../auth-token";

export const authMeKey = ["auth", "me"] as const;
export const roleKey = ["auth", "whoami"] as const;

/** Who is signed in, whatever their kind (shopper, merchant, business). */
export function useWhoAmI() {
  return useQuery({
    queryKey: roleKey,
    queryFn: authApi.whoami,
    enabled: !!getToken(),
    retry: false,
    staleTime: 60_000,
  });
}

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
      queryClient.removeQueries({ queryKey: roleKey });
      if (customer) queryClient.setQueryData(authMeKey, customer);
    },
  });
}

export function useRegister() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: authApi.RegisterInput) => authApi.register(input),
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
    queryClient.removeQueries({ queryKey: roleKey });
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
