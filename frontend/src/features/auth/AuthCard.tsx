import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useLogin, useRegister } from "@/lib/hooks/useAuth";

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
}: {
  onAuthed?: () => void;
  className?: string;
}) {
  const [mode, setMode] = useState<Mode>("login");
  const login = useLogin();
  const register = useRegister();

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: { email: "", password: "", name: "" },
  });

  const pending = login.isPending || register.isPending;
  const serverError = login.error?.message ?? register.error?.message;

  async function onSubmit(values: z.infer<typeof formSchema>) {
    if (mode === "login") {
      await login.mutateAsync({ email: values.email, password: values.password });
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
      await register.mutateAsync({ email: values.email, password: values.password, name });
    }
    onAuthed?.();
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
        <button
          type="button"
          onClick={() => setMode("register")}
          className={`flex-1 rounded-md py-1.5 font-medium transition-colors ${
            mode === "register" ? "bg-tangerine text-ink" : "text-cream/70"
          }`}
        >
          Create account
        </button>
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
