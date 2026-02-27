"use client";

import { usePathname } from "next/navigation";

import { TopNav } from "@/components/top-nav";

export function NavShell() {
  const pathname = usePathname();
  if (pathname === "/") return null;
  return <TopNav />;
}
