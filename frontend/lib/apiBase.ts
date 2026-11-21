/** Where the API lives: an absolute origin in the cross-origin dev compose, a
 *  relative `/api` behind the same-origin production edge. Its own module so
 *  the session layer (auth.ts) and the endpoint catalogue (api.ts) can both
 *  read it without importing each other. */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
