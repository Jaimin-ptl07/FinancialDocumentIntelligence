/** Where login sends the analyst afterwards, taken from `?next=`.
 *
 *  `next` arrives in a URL anyone can craft, so it is honoured only as a path
 *  on THIS site. Prefix checks alone are not enough: "//evil.example" is
 *  protocol-relative, and browsers strip tab/CR/LF while parsing, so
 *  "/<TAB>/evil.example" becomes the same thing. So the value is parsed the way
 *  the browser will parse it, and kept only if it stays on this origin. /login
 *  itself (a loop) and anything unparseable fall back to the filings list. */
export function safeReturnPath(
  next: string | null,
  origin: string = window.location.origin,
): string {
  if (!next || !next.startsWith("/")) return "/";
  let url: URL;
  try {
    url = new URL(next, origin);
  } catch {
    return "/";
  }
  if (url.origin !== origin || url.pathname === "/login") return "/";
  return `${url.pathname}${url.search}${url.hash}`;
}

export function loginHref(returnTo: string): string {
  return returnTo === "/" ? "/login" : `/login?next=${encodeURIComponent(returnTo)}`;
}
