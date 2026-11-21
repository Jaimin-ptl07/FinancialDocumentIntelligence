import "@testing-library/jest-dom/vitest";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { handlers } from "./handlers";

export const server = setupServer(...handlers);

/** `usePathname` returns null outside a real App Router tree, which would make
 *  every component that reads it throw under test. Tests set the path they are
 *  pretending to be on via `setPathname`. */
let pathname = "/";
export function setPathname(next: string) {
  pathname = next;
}

/** Likewise the query string, for pages that read `useSearchParams`. */
let searchParams = new URLSearchParams();
export function setSearchParams(next: string) {
  searchParams = new URLSearchParams(next);
}

/** One router for the whole test, so a test can assert where a component
 *  navigated (`expect(router.replace).toHaveBeenCalledWith(...)`). Its spies
 *  are reset after each test. */
export const router = {
  push: vi.fn(),
  replace: vi.fn(),
  back: vi.fn(),
  refresh: vi.fn(),
  prefetch: vi.fn(),
};

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => router,
  useSearchParams: () => searchParams,
  useParams: () => ({}),
}));

// jsdom has no matchMedia implementation at all. Components read it to sense
// prefers-color-scheme (see Spine's theme toggle); without this, any such
// read throws "window.matchMedia is not a function" the moment a test
// mounts them, regardless of what the test is actually checking.
if (typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => false),
  });
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  cleanup();
  pathname = "/";
  searchParams = new URLSearchParams();
  Object.values(router).forEach((spy) => spy.mockReset());
});
afterAll(() => server.close());
