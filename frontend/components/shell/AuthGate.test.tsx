import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { listDocuments } from "@/lib/api";
import { clearSession, hasSession, login } from "@/lib/auth";
import { router, server, setPathname, setSearchParams } from "@/test/setup";
import { AuthGate } from "./AuthGate";

const API = "http://localhost:8000";

function renderGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthGate>
        <p>the app</p>
      </AuthGate>
    </QueryClientProvider>,
  );
}

function grantSessions({ refresh }: { refresh: boolean }) {
  server.use(
    http.post(`${API}/auth/login`, () =>
      HttpResponse.json({ access_token: "t", token_type: "bearer", expires_in: 900 }),
    ),
    http.post(`${API}/auth/refresh`, () =>
      refresh
        ? HttpResponse.json({ access_token: "t2", token_type: "bearer", expires_in: 900 })
        : HttpResponse.json({ detail: "No refresh token was presented." }, { status: 401 }),
    ),
    http.post(`${API}/auth/logout`, () => new HttpResponse(null, { status: 204 })),
  );
}

afterEach(() => clearSession());

describe("AuthGate", () => {
  it("sends a visitor with no session to login, remembering where they were going", async () => {
    grantSessions({ refresh: false });
    setPathname("/documents/doc-1");
    setSearchParams("tab=fog");
    renderGate();

    await waitFor(() =>
      expect(router.replace).toHaveBeenCalledWith(
        "/login?next=%2Fdocuments%2Fdoc-1%3Ftab%3Dfog",
      ),
    );
    expect(screen.queryByText("the app")).not.toBeInTheDocument();
  });

  it("lets a returning visitor straight in when the refresh cookie still holds", async () => {
    grantSessions({ refresh: true });
    renderGate();

    expect(await screen.findByText("the app")).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("does not mistake an unreachable API for a signed-out visitor", async () => {
    server.use(
      http.post(`${API}/auth/refresh`, () =>
        HttpResponse.json({ detail: "down" }, { status: 503 }),
      ),
    );
    renderGate();

    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot reach/i);
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("renders the login page bare, outside the gate and the shell", () => {
    setPathname("/login");
    renderGate();

    expect(screen.getByText("the app")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Sections" })).not.toBeInTheDocument();
  });

  it("returns to login when the session dies mid-use and cannot be renewed", async () => {
    grantSessions({ refresh: false });
    await login("ana@example.com", "pw");
    setPathname("/costing");
    renderGate();
    expect(screen.getByText("the app")).toBeInTheDocument();

    server.use(
      http.get(`${API}/documents`, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    await expect(listDocuments()).rejects.toThrow();

    await waitFor(() =>
      expect(router.replace).toHaveBeenCalledWith("/login?next=%2Fcosting"),
    );
  });

  it("signs out: revokes the session and lands on login", async () => {
    grantSessions({ refresh: false });
    let revoked = false;
    server.use(
      http.post(`${API}/auth/logout`, () => {
        revoked = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    await login("ana@example.com", "pw");
    renderGate();

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(revoked).toBe(true);
  });

  it("signs out locally even when the API cannot be told", async () => {
    grantSessions({ refresh: false });
    server.use(http.post(`${API}/auth/logout`, () => HttpResponse.error()));
    await login("ana@example.com", "pw");
    renderGate();

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(hasSession()).toBe(false);
  });

  it("does not carry the signed-out page into the next sign-in", async () => {
    grantSessions({ refresh: false });
    await login("ana@example.com", "pw");
    setPathname("/documents/doc-1");
    renderGate();
    // A query still in flight as the session ends: its 401 cannot be renewed.
    server.use(
      http.post(`${API}/auth/logout`, async () => {
        await listDocuments().catch(() => undefined);
        return new HttpResponse(null, { status: 204 });
      }),
      http.get(`${API}/documents`, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(router.replace).not.toHaveBeenCalledWith("/login?next=%2Fdocuments%2Fdoc-1");
  });
});
