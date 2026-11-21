"use client";

import { useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { hasSession, logout, onSessionExpired, restoreSession } from "@/lib/auth";
import { loginHref } from "@/lib/returnTo";
import { Spine } from "./Spine";

/** The login wall in the browser (T4), wrapped around the app shell.
 *
 *  Client-side because it has to be: the refresh cookie is httpOnly and
 *  scoped to the API's /auth path, so the Next server never sees it and
 *  cannot decide anything. The API is the real gate (every route 401s
 *  without a bearer); this only decides what to SHOW -- the app, or a trip
 *  to /login carrying the address to come back to.
 *
 *  /login renders bare: no shell, no session check, nothing to loop on. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  if (pathname === "/login") return <>{children}</>;
  return <GatedShell pathname={pathname}>{children}</GatedShell>;
}

function GatedShell({
  pathname,
  children,
}: {
  pathname: string;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const search = useSearchParams().toString();
  const queryClient = useQueryClient();
  const [ready, setReady] = useState(hasSession);
  const [unreachable, setUnreachable] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // Set for the duration of a sign-out: queries still in flight will 401 and
  // fail to renew, and their expiry signal must not redirect to
  // `/login?next=<this page>` -- the NEXT person to sign in would land on it.
  const signingOut = useRef(false);

  const here = search ? `${pathname}?${search}` : pathname;
  const toLogin = useCallback(() => {
    if (!signingOut.current) router.replace(loginHref(here));
  }, [router, here]);

  // First visit or a reload: the access token is gone from memory, but the
  // refresh cookie may survive. Trade it in before deciding. An API that
  // cannot be reached is said so, with a retry -- not taken as "signed out".
  useEffect(() => {
    if (ready) return;
    let cancelled = false;
    restoreSession().then(
      (restored) => {
        if (cancelled) return;
        if (restored) setReady(true);
        else toLogin();
      },
      () => {
        if (!cancelled) setUnreachable(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [ready, toLogin, attempt]);

  // Mid-use: a call hit 401 and the silent refresh was refused.
  useEffect(() => onSessionExpired(toLogin), [toLogin]);

  const signOut = async () => {
    signingOut.current = true;
    try {
      await logout();
    } catch {
      // The local session is already gone (logout clears it whatever the
      // network says); the cookie the server never heard about expires.
    }
    // The next Invitee on this browser must not see the last one's cache.
    queryClient.clear();
    router.replace("/login");
  };

  if (unreachable) {
    return (
      <div style={{ padding: "var(--sp-8)" }}>
        <p role="alert" style={{ fontSize: "var(--text-sm)", marginBottom: "var(--sp-3)" }}>
          Cannot reach the server to check your session.
        </p>
        <button
          type="button"
          className="btn"
          onClick={() => {
            setUnreachable(false);
            setAttempt((n) => n + 1);
          }}
        >
          Try again
        </button>
      </div>
    );
  }
  if (!ready) {
    return (
      <p className="eyebrow" style={{ padding: "var(--sp-8)" }}>
        Checking your session
      </p>
    );
  }
  return (
    <div className="app-shell">
      <Spine onSignOut={signOut} />
      <main className="flex-1 min-w-0">{children}</main>
    </div>
  );
}
