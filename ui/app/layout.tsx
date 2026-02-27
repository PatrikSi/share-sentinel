import type { Metadata } from "next";

import { NavShell } from "./nav-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Share Sentinel",
  description: "SMB enumeration data platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <NavShell />
        <main className="mx-auto max-w-7xl px-4 pb-8">{children}</main>
      </body>
    </html>
  );
}
