import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { retryUnlessAnswered } from "./providers";

describe("retryUnlessAnswered", () => {
  it("does not retry a 404 — the stage has not run, and asking again cannot change that", () => {
    const notFound = new ApiError("GET /documents/x/artifacts/fog_reason failed (404)", 404);
    expect(retryUnlessAnswered(0, notFound)).toBe(false);
  });

  it("does not retry any other 4xx either", () => {
    for (const status of [400, 401, 403, 409, 422]) {
      expect(retryUnlessAnswered(0, new ApiError("nope", status))).toBe(false);
    }
  });

  it("retries a 5xx once — the server may answer differently in a moment", () => {
    const boom = new ApiError("GET /documents failed (503)", 503);
    expect(retryUnlessAnswered(0, boom)).toBe(true);
    expect(retryUnlessAnswered(1, boom)).toBe(false);
  });

  it("retries a network failure, which carries no status at all", () => {
    expect(retryUnlessAnswered(0, new TypeError("Failed to fetch"))).toBe(true);
  });
});
