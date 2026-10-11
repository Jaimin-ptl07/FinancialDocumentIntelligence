import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpResponse, http } from "msw";
import { server } from "@/test/setup";
import { listDocuments } from "./api";
import {
  clearSession,
  freshAuthHeaders,
  hasSession,
  login,
  logout,
  onSessionExpired,
  restoreSession,
} from "./auth";

const API = "http://localhost:8000";

/** Grants `token` on login/refresh, and gates GET /documents on it. */
function backend({ refreshToken = "refreshed", refreshStatus = 200 } = {}) {
  const calls = { refresh: 0, logout: 0, authHeaders: [] as (string | null)[] };
  let valid = "first";
  server.use(
    http.post(`${API}/auth/login`, async ({ request }) => {
      const body = (await request.json()) as { email: string; password: string };
      if (body.password !== "right") {
        return HttpResponse.json({ detail: "Invalid credentials." }, { status: 401 });
      }
      return HttpResponse.json({ access_token: "first", token_type: "bearer", expires_in: 900 });
    }),
    http.post(`${API}/auth/refresh`, () => {
      calls.refresh += 1;
      if (refreshStatus !== 200) {
        return HttpResponse.json({ detail: "No refresh token." }, { status: refreshStatus });
      }
      valid = refreshToken;
      return HttpResponse.json({ access_token: refreshToken, token_type: "bearer", expires_in: 900 });
    }),
    http.post(`${API}/auth/logout`, () => {
      calls.logout += 1;
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/documents`, ({ request }) => {
      const header = request.headers.get("authorization");
      calls.authHeaders.push(header);
      if (header !== `Bearer ${valid}`) {
        return HttpResponse.json({ detail: "expired" }, { status: 401 });
      }
      return HttpResponse.json({ documents: [], total: 0, limit: 50, offset: 0 });
    }),
  );
  return { calls, expireAccessToken: () => (valid = "rotated-away") };
}

afterEach(() => clearSession());

describe("login", () => {
  it("opens a session whose token rides every API call", async () => {
    const { calls } = backend();
    await login("ana@example.com", "right");
    expect(hasSession()).toBe(true);
    await listDocuments();
    expect(calls.authHeaders).toEqual(["Bearer first"]);
  });

  it("refuses wrong credentials with the backend's reason and opens nothing", async () => {
    backend();
    await expect(login("ana@example.com", "wrong")).rejects.toThrow(/Invalid credentials/);
    expect(hasSession()).toBe(false);
  });
});

describe("an expired access token", () => {
  it("is refreshed silently and the call succeeds", async () => {
    const { calls, expireAccessToken } = backend();
    await login("ana@example.com", "right");
    expireAccessToken();

    const result = await listDocuments();

    expect(result.total).toBe(0);
    expect(calls.refresh).toBe(1);
    expect(calls.authHeaders).toEqual(["Bearer first", "Bearer refreshed"]);
  });

  it("is refreshed once for many concurrent calls — rotation burns the cookie", async () => {
    const { calls, expireAccessToken } = backend();
    await login("ana@example.com", "right");
    expireAccessToken();

    await Promise.all([listDocuments(), listDocuments(), listDocuments()]);

    expect(calls.refresh).toBe(1);
  });

  it("ends the session and says so when the refresh is refused", async () => {
    backend({ refreshStatus: 401 });
    const expired = vi.fn();
    const unsubscribe = onSessionExpired(expired);
    await login("ana@example.com", "right");
    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    await expect(listDocuments()).rejects.toMatchObject({ status: 401 });

    expect(expired).toHaveBeenCalledTimes(1);
    expect(hasSession()).toBe(false);
    unsubscribe();
  });
});

describe("restoreSession", () => {
  it("turns a surviving refresh cookie into a session after a reload", async () => {
    backend();
    expect(await restoreSession()).toBe(true);
    expect(hasSession()).toBe(true);
  });

  it("tells an unreachable API apart from an absent session", async () => {
    backend({ refreshStatus: 503 });
    await expect(restoreSession()).rejects.toMatchObject({ status: 503 });
  });

  it("reports no session when there is no cookie to refresh", async () => {
    backend({ refreshStatus: 401 });
    expect(await restoreSession()).toBe(false);
    expect(hasSession()).toBe(false);
  });
});

describe("freshAuthHeaders", () => {
  it("hands out the current bearer for loaders that cannot go through the client", async () => {
    backend();
    await login("ana@example.com", "right");
    expect(await freshAuthHeaders()).toEqual({ Authorization: "Bearer first" });
  });

  it("announces the end of the session when it cannot renew one", async () => {
    backend({ refreshStatus: 401 });
    const expired = vi.fn();
    const unsubscribe = onSessionExpired(expired);

    expect(await freshAuthHeaders()).toEqual({});

    expect(expired).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});

describe("logout", () => {
  it("revokes the cookie server-side and forgets the token", async () => {
    const { calls } = backend();
    await login("ana@example.com", "right");
    await logout();
    expect(calls.logout).toBe(1);
    expect(hasSession()).toBe(false);
  });
});
