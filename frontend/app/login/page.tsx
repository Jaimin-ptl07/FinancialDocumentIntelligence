"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { login } from "@/lib/auth";
import { safeReturnPath } from "@/lib/returnTo";

/** The Gated demo's front door (ADR-0002). Accounts are made by the admin
 *  CLI, so there is no sign-up or reset link here -- an Invitee who cannot get
 *  in asks the operator. */
function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(email, password);
      router.replace(safeReturnPath(next));
    } catch (cause) {
      setError((cause as Error).message || "Sign-in failed.");
      setPending(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="sheet flex flex-col"
      style={{ width: "100%", maxWidth: "22rem", padding: "var(--sp-8)", gap: "var(--sp-4)" }}
    >
      <div>
        <div className="eyebrow">Financial document</div>
        <h1 className="display" style={{ fontSize: "var(--text-lg)", marginTop: "var(--sp-1)" }}>
          Intelligence
        </h1>
      </div>

      <label className="flex flex-col" style={{ gap: "var(--sp-1)", fontSize: "var(--text-sm)" }}>
        Email
        <input
          className="input"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>

      <label className="flex flex-col" style={{ gap: "var(--sp-1)", fontSize: "var(--text-sm)" }}>
        Password
        <input
          className="input"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>

      {error ? (
        <p role="alert" style={{ fontSize: "var(--text-sm)", color: "var(--ink)" }}>
          {error}
        </p>
      ) : null}

      <button type="submit" className="btn btn-primary justify-center" disabled={pending}>
        {pending ? "Signing in" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <div
      className="flex items-center justify-center"
      style={{ minHeight: "100vh", padding: "var(--sp-4)" }}
    >
      {/* useSearchParams opts the page out of static prerender unless it sits
          under a Suspense boundary. */}
      <Suspense>
        <LoginForm />
      </Suspense>
    </div>
  );
}
