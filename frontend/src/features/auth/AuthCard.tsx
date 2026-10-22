import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useQuery } from "@tanstack/react-query";

import { useLogin, useRegister } from "@/lib/hooks/useAuth";
import type { Role } from "@/lib/api/auth";
import { DEFAULT_LANGUAGE, getConsentText, LANGUAGE_LABEL, sendRegisterOtp, verifyRegisterOtp, type Language } from "@/lib/api/contact";

import { PhoneVerify } from "./PhoneVerify";

// One schema shared by both modes (dynamically swapping the zod resolver's
// output type per mode fights react-hook-form's generics under
// exactOptionalPropertyTypes) — `name` stays optional here and is validated
// manually for register mode in onSubmit instead.
const formSchema = z.object({
  name: z.string().optional(),
  email: z.string().min(1, "Email is required").email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
});

type Mode = "login" | "register";

export function AuthCard({
  onAuthed,
  className = "",
  initialMode = "login",
  allowRegister = true,
}: {
  onAuthed?: (role: Role) => void;
  className?: string;
  initialMode?: Mode;
  /** Only shoppers can sign themselves up; merchant and business accounts are set up for them. */
  allowRegister?: boolean;
}) {
  const [mode, setMode] = useState<Mode>(allowRegister ? initialMode : "login");
  const login = useLogin();
  const register = useRegister();

  // Registration needs a phone that has passed the OTP check. The proof the
  // server issues is bound to that exact number; editing the number drops it.
  const [verifiedPhone, setVerifiedPhone] = useState<string | null>(null);
  const [phoneProof, setPhoneProof] = useState<string | null>(null);
  const [language, setLanguage] = useState<Language>(DEFAULT_LANGUAGE);
  const [consent, setConsent] = useState(false);
  const [phoneError, setPhoneError] = useState<string | null>(null);
  const consentQ = useQuery({ queryKey: ["contacts", "consent-text"], queryFn: getConsentText, staleTime: Infinity });

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: { email: "", password: "", name: "" },
  });

  const pending = login.isPending || register.isPending;
  const serverError = login.error?.message ?? register.error?.message;

  async function onSubmit(values: z.infer<typeof formSchema>) {
    if (mode === "login") {
      const r = await login.mutateAsync({ email: values.email, password: values.password });
      onAuthed?.(r.role);
      return;
    } else {
      const name = values.name?.trim();
      if (!name) {
        form.setError("name", { message: "Name is required" });
        return;
      }
      if (values.password.length < 8) {
        form.setError("password", { message: "Password must be at least 8 characters" });
        return;
      }
      if (!verifiedPhone || !phoneProof) {
        setPhoneError("Verify your mobile number to create an account.");
        return;
      }
      setPhoneError(null);
      await register.mutateAsync({
        email: values.email,
        password: values.password,
        name,
        phone: verifiedPhone,
        phone_proof: phoneProof,
        preferred_language: language,
        voice_consent: consent,
      });
    }
    onAuthed?.("customer");
  }

  return (
    <div className={className}>
      <div className="flex items-center gap-2 mb-6 rounded-lg bg-cream/10 p-1 text-sm">
        <button
          type="button"
          onClick={() => setMode("login")}
          className={`flex-1 rounded-md py-1.5 font-medium transition-colors ${
            mode === "login" ? "bg-tangerine text-ink" : "text-cream/70"
          }`}
        >
          Sign in
        </button>
        {allowRegister && (
          <button
            type="button"
            onClick={() => setMode("register")}
            className={`flex-1 rounded-md py-1.5 font-medium transition-colors ${
              mode === "register" ? "bg-tangerine text-ink" : "text-cream/70"
            }`}
          >
            Create account
          </button>
        )}
      </div>

      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          {mode === "register" && (
            <FormField
              control={form.control}
              name="name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel className="text-xs uppercase tracking-[0.15em] text-cream/60">Name</FormLabel>
                  <FormControl>
                    <Input
                      placeholder="Priya Sharma"
                      className="bg-cream/10 border-cream/20 text-cream placeholder:text-cream/40 focus-visible:ring-tangerine"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
          )}
          <FormField
            control={form.control}
            name="email"
            render={({ field }) => (
              <FormItem>
                <FormLabel className="text-xs uppercase tracking-[0.15em] text-cream/60">Email</FormLabel>
                <FormControl>
                  <Input
                    type="email"
                    placeholder="priya@example.com"
                    className="bg-cream/10 border-cream/20 text-cream placeholder:text-cream/40 focus-visible:ring-tangerine"
                    {...field}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={form.control}
            name="password"
            render={({ field }) => (
              <FormItem>
                <FormLabel className="text-xs uppercase tracking-[0.15em] text-cream/60">Password</FormLabel>
                <FormControl>
                  <Input
                    type="password"
                    placeholder="••••••••"
                    className="bg-cream/10 border-cream/20 text-cream placeholder:text-cream/40 focus-visible:ring-tangerine"
                    {...field}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          {mode === "register" && (
            <div className="space-y-4">
              <PhoneVerify
                variant="dark"
                purposeLine={consentQ.data?.phone_purpose}
                send={sendRegisterOtp}
                verify={async (phone, code) => (await verifyRegisterOtp(phone, code)).phone_proof}
                onVerified={(phone, proof) => {
                  setVerifiedPhone(phone);
                  setPhoneProof(proof ?? null);
                  setPhoneError(null);
                }}
                onReset={() => {
                  setVerifiedPhone(null);
                  setPhoneProof(null);
                  setConsent(false);
                }}
              />
              {phoneError && <p className="text-sm text-failed">{phoneError}</p>}

              <div>
                <label className="block text-xs uppercase tracking-[0.15em] text-cream/60 mb-1.5">
                  Preferred language <span className="normal-case tracking-normal text-cream/40">(optional)</span>
                </label>
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value as Language)}
                  className="w-full rounded-md bg-cream/10 border border-cream/20 text-cream px-3 py-2 text-sm"
                >
                  {(Object.keys(LANGUAGE_LABEL) as Language[]).map((l) => (
                    <option key={l} value={l} className="text-ink">
                      {LANGUAGE_LABEL[l]}
                    </option>
                  ))}
                </select>
              </div>

              {/* Starts unchecked and is never required: registering does not
                  depend on it, and it can only be ticked for a verified number. */}
              <label className={`flex items-start gap-2.5 text-xs leading-relaxed ${verifiedPhone ? "text-cream/80" : "text-cream/40"}`}>
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={consent}
                  disabled={!verifiedPhone}
                  onChange={(e) => setConsent(e.target.checked)}
                />
                <span>
                  {consentQ.data?.text ?? "You may call me about a payment of mine that failed."}{" "}
                  <span className="text-cream/40">(optional — you can change this any time)</span>
                </span>
              </label>
            </div>
          )}

          {serverError && <p className="text-sm text-failed">{serverError}</p>}

          <Button
            type="submit"
            disabled={pending}
            className="w-full rounded-lg bg-tangerine text-ink font-semibold py-3 h-auto shadow-[0_18px_36px_-14px_rgba(245,158,11,0.7)] hover:bg-tangerine/90"
          >
            {pending ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
      </Form>
    </div>
  );
}
