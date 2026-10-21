import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { StatusHeader } from "./status-header";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "fantasy-rag",
  description: "Fantasy football stats, comparisons and lineups from nflverse and Sleeper data",
};

const NAV = [
  ["/", "Dashboard"],
  ["/players", "Players"],
  ["/compare", "Compare"],
  ["/chat", "Ask"],
  ["/league", "My league"],
  ["/model", "Model"],
] as const;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col">
        <header className="border-b">
          <nav className="mx-auto flex max-w-5xl items-center gap-6 px-4 py-3 text-sm">
            <span className="font-semibold">fantasy-rag</span>
            {NAV.map(([href, label]) => (
              <Link key={href} href={href} className="text-muted-foreground hover:text-foreground">
                {label}
              </Link>
            ))}
          </nav>
        </header>
        <StatusHeader />
        <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">{children}</main>
        <footer className="border-t px-4 py-3 text-center text-xs text-muted-foreground">
          Data from nflverse (CC-BY 4.0) and Sleeper. Projections: baseline model; byes and Out/IR count as 0.
        </footer>
      </body>
    </html>
  );
}
