import type { Metadata } from "next";
import { Archivo, IBM_Plex_Mono, Public_Sans } from "next/font/google";
import "./globals.css";
import { Spine } from "@/components/shell/Spine";
import { Providers } from "./providers";

const archivo = Archivo({
  variable: "--font-archivo",
  subsets: ["latin"],
  axes: ["wdth"],
});

const publicSans = Public_Sans({
  variable: "--font-public-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Financial Document Intelligence",
  description: "Read, extract, and verify figures from annual reports.",
};

// Stamps data-theme on <html> before Providers/Spine hydrate, straight from
// localStorage. Runs synchronously in <head>, ahead of first paint, so a
// returning visitor with a saved preference never sees one theme flash to
// the other. No preference saved -> no attribute set -> the plain
// prefers-color-scheme media query in globals.css governs, same as before
// this toggle existed.
const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("fdi-theme");if(t==="dark"||t==="light"){document.documentElement.setAttribute("data-theme",t);}}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${archivo.variable} ${publicSans.variable} ${plexMono.variable} h-full`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full">
        <Providers>
          <div className="app-shell">
            <Spine />
            <main className="flex-1 min-w-0">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
