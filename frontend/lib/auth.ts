import { API_BASE } from "./apiBase";

/** The browser half of the ADR-0002 session.
 *
 *  The ACCESS token (~15 min) lives only in this module's memory: never in
 *  localStorage, where any injected script could lift it. The REFRESH token
 *  is an httpOnly cookie this code cannot read at all -- it only asks the
 *  browser to send it (`credentials: "include"`) to /auth/*. A reload
 *  therefore forgets the access token, and `restoreSession` trades the
 *  surviving cookie for a new one.
 *
 *  Rotation burns the presented cookie, so two refreshes racing on one cookie
 *  would see the second refused and log the analyst out mid-demo. Every
 *  refresh therefore goes through one in-flight promise. */

/** A failed API call, carrying its HTTP status as DATA rather than only as
 *  text inside the message.
 *
 *  Callers have to branch on it. A 404 from `getArtifact` means "this stage has
 *  not run for this filing" — a normal state for a filing mid-ingest, and one
 *  that no amount of retrying can change. Deciding that from a regex over the
 *  message would make the retry policy depend on the wording of an error
 *  string, which is exactly the kind of coupling that breaks silently when
 *  someone improves the wording. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The backend's own `detail` string, or the status text when the body is
 *  not the problem+json shape. */
export async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* body was not JSON; the status text stands */
  }
  return response.statusText;
}

type TokenView = { access_token: string; expires_in: number };

let accessToken: string | null = null;
let expiresAt = 0;
let inflightRefresh: Promise<boolean> | null = null;
const expiryListeners = new Set<() => void>();

// Refresh a little before the server would refuse, so a loader that cannot
// retry (pdf.js) is not handed a token with seconds left on it.
const EXPIRY_MARGIN_MS = 30_000;

function adopt(view: TokenView) {
  accessToken = view.access_token;
  expiresAt = Date.now() + view.expires_in * 1000;
}

export function hasSession(): boolean {
  return accessToken !== null;
}

/** Forget the access token locally. Does not touch the cookie -- see logout. */
export function clearSession() {
  accessToken = null;
  expiresAt = 0;
}

/** Subscribe to "the session is gone and could not be renewed". The gate
 *  uses it to send the analyst to the login page. */
export function onSessionExpired(handler: () => void): () => void {
  expiryListeners.add(handler);
  return () => expiryListeners.delete(handler);
}

function authFetch(path: string, init?: RequestInit) {
  return fetch(`${API_BASE}/auth/${path}`, {
    method: "POST",
    credentials: "include",
    ...init,
  });
}

export async function login(email: string, password: string): Promise<void> {
  const response = await authFetch("login", {
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new ApiError(await detailOf(response), response.status);
  }
  adopt((await response.json()) as TokenView);
}

/** POST /auth/refresh, serialised ACROSS TABS where the browser allows it:
 *  the in-flight promise below only dedupes within one tab, and two tabs
 *  presenting the same cookie at once would see the loser refused. Under the
 *  lock, a waiting tab sends whatever cookie the jar holds by then -- the
 *  winner's rotated one -- and rotates it again, legitimately. */
async function postRefresh(): Promise<Response> {
  if (typeof navigator !== "undefined" && navigator.locks) {
    // lib.dom types the result as Promise<Promise<Response>>; await flattens.
    return await navigator.locks.request("fdi-auth-refresh", () => authFetch("refresh"));
  }
  return authFetch("refresh");
}

/** Trade the refresh cookie for a new access token. Single-flight: callers
 *  arriving while a refresh is out share its answer.
 *
 *  Resolves false only when the server says there is no session (401/403).
 *  An unreachable or failing API REJECTS instead and leaves any token in
 *  place: a blip during a reload is not a logout, and treating it as one
 *  would make the analyst re-enter a password for a cookie that is fine. */
export function restoreSession(): Promise<boolean> {
  inflightRefresh ??= (async () => {
    try {
      const response = await postRefresh();
      if (response.status === 401 || response.status === 403) {
        clearSession();
        return false;
      }
      if (!response.ok) {
        throw new ApiError(await detailOf(response), response.status);
      }
      adopt((await response.json()) as TokenView);
      return true;
    } finally {
      inflightRefresh = null;
    }
  })();
  return inflightRefresh;
}

function announceExpiry() {
  expiryListeners.forEach((handler) => handler());
}

/** Revoke the refresh cookie server-side, then forget the access token. The
 *  local half happens even if the network call fails: a logout button that
 *  leaves you logged in is worse than one that leaves a cookie to expire. */
export async function logout(): Promise<void> {
  try {
    await authFetch("logout");
  } finally {
    clearSession();
  }
}

function withBearer(init?: RequestInit): RequestInit {
  const headers = new Headers(init?.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return { ...init, headers };
}

/** fetch() against the API with the bearer attached. A 401 gets exactly one
 *  silent refresh-and-retry; if the refresh is refused the session is over,
 *  the expiry listeners hear about it, and the 401 goes back to the caller.
 *
 *  The retry re-sends `init` as given, so its body must be re-readable
 *  (string, FormData, Blob) -- a one-shot ReadableStream body would already
 *  be consumed by the first attempt. */
export async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const tokenUsed = accessToken;
  const response = await fetch(`${API_BASE}${path}`, withBearer(init));
  if (response.status !== 401) return response;

  // Another call may already have rotated the token while this one was out;
  // only refresh if the token we were refused with is still the current one.
  const renewed = accessToken !== tokenUsed && accessToken !== null
    ? true
    : await restoreSession();
  if (!renewed) {
    announceExpiry();
    return response;
  }
  return fetch(`${API_BASE}${path}`, withBearer(init));
}

/** The Authorization header for loaders that fetch on their own (pdf.js),
 *  renewed first if the token is missing or about to expire -- those loaders
 *  cannot run the 401 retry above, so a refused renewal is announced here
 *  instead, exactly as authorizedFetch would. An unreachable API is not:
 *  the loader goes ahead with what it has and reports its own failure. */
export async function freshAuthHeaders(): Promise<Record<string, string>> {
  if (accessToken === null || Date.now() >= expiresAt - EXPIRY_MARGIN_MS) {
    const renewed = await restoreSession().catch(() => accessToken !== null);
    if (!renewed) announceExpiry();
  }
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}
