import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { clearSession, hasSession } from "@/lib/auth";
import { router, server, setSearchParams } from "@/test/setup";
import LoginPage from "./page";

const API = "http://localhost:8000";

function acceptOnly(password: string) {
  server.use(
    http.post(`${API}/auth/login`, async ({ request }) => {
      const body = (await request.json()) as { email: string; password: string };
      return body.password === password
        ? HttpResponse.json({ access_token: "t", token_type: "bearer", expires_in: 900 })
        : HttpResponse.json({ detail: "Invalid credentials." }, { status: 401 });
    }),
  );
}

async function signIn(password: string) {
  const user = userEvent.setup();
  render(<LoginPage />);
  // click + paste, not type(): per-keystroke events made the first (cold)
  // test of the file brush the 5s timeout under a parallel suite run.
  await user.click(await screen.findByLabelText("Email"));
  await user.paste("ana@example.com");
  await user.click(screen.getByLabelText("Password"));
  await user.paste(password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => clearSession());

describe("LoginPage", () => {
  it("signs the analyst in and returns them to where they were going", async () => {
    acceptOnly("right");
    setSearchParams("next=/documents/doc-1");

    await signIn("right");

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/documents/doc-1"));
    expect(hasSession()).toBe(true);
  });

  it("lands on the filings list when there is nowhere to return to", async () => {
    acceptOnly("right");

    await signIn("right");

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/"));
  });

  it("never follows a return address off this site", async () => {
    acceptOnly("right");
    setSearchParams("next=//evil.example/phish");

    await signIn("right");

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/"));
  });

  it("is not fooled by a control character smuggled between the slashes", async () => {
    acceptOnly("right");
    // Browsers strip tab/newline while parsing a URL, so "/<TAB>/evil.example"
    // would navigate as "//evil.example" -- protocol-relative, off-site.
    setSearchParams("next=/%09/evil.example");

    await signIn("right");

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/"));
  });

  it("says why a refused sign-in failed and stays put", async () => {
    acceptOnly("right");

    await signIn("wrong");

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid credentials.");
    expect(router.replace).not.toHaveBeenCalled();
    expect(hasSession()).toBe(false);
  });
});
