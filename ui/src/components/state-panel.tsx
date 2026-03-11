import { ReactNode } from "react";

type StatePanelTone = "neutral" | "warning" | "error";

const PANEL_CLASSES: Record<StatePanelTone, string> = {
  neutral: "border-slate-300 bg-slate-50/80 text-slate-600 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300",
  warning: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-200",
  error: "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/40 dark:bg-rose-900/20 dark:text-rose-200",
};

type StatePanelProps = {
  title: string;
  description: string;
  tone?: StatePanelTone;
  actions?: ReactNode;
};

export function StatePanel({ title, description, tone = "neutral", actions }: StatePanelProps) {
  return (
    <div className={`rounded-3xl border border-dashed px-6 py-8 text-center ${PANEL_CLASSES[tone]}`}>
      <p className="text-sm font-semibold uppercase tracking-[0.18em]">{title}</p>
      <p className="mt-3 text-sm">{description}</p>
      {actions ? <div className="mt-4 flex justify-center">{actions}</div> : null}
    </div>
  );
}
