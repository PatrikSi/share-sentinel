import { ReactNode } from "react";

type StatusBannerTone = "error" | "success" | "warning" | "info";

const TONE_CLASSES: Record<StatusBannerTone, string> = {
  error: "border-rose-200 bg-rose-100 text-rose-700 dark:border-rose-900/40 dark:bg-rose-900/20 dark:text-rose-200",
  success: "border-emerald-200 bg-emerald-100 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-900/20 dark:text-emerald-200",
  warning: "border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200",
  info: "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-200",
};

type StatusBannerProps = {
  title?: string;
  tone?: StatusBannerTone;
  children: ReactNode;
};

export function StatusBanner({ title, tone = "info", children }: StatusBannerProps) {
  return (
    <div className={`rounded-2xl border p-3 text-sm ${TONE_CLASSES[tone]}`}>
      {title ? <p className="text-[11px] font-semibold uppercase tracking-[0.18em]">{title}</p> : null}
      <div className={title ? "mt-2" : ""}>{children}</div>
    </div>
  );
}
