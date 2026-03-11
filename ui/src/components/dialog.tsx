import { ReactNode, useEffect } from "react";

type DialogProps = {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
};

const SIZE_CLASSES: Record<NonNullable<DialogProps["size"]>, string> = {
  sm: "max-w-md",
  md: "max-w-2xl",
  lg: "max-w-4xl",
};

export function Dialog({ open, title, description, onClose, children, footer, size = "md" }: DialogProps) {
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 px-4 py-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div className={`w-full ${SIZE_CLASSES[size]} rounded-[28px] border border-slate-200 bg-white p-6 shadow-2xl dark:border-slate-800 dark:bg-slate-950`}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-500">Confirm Action</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight">{title}</h2>
            {description ? <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{description}</p> : null}
          </div>
          <button
            className="rounded-2xl border border-slate-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
            onClick={onClose}
            type="button"
          >
            Close
          </button>
        </div>

        <div className="mt-6">{children}</div>
        {footer ? <div className="mt-6 flex flex-wrap items-center justify-end gap-3">{footer}</div> : null}
      </div>
    </div>
  );
}
