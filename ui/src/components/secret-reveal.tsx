import { useState } from "react";

import { StatusBanner } from "@/components/status-banner";

type SecretRevealProps = {
  label: string;
  secret: string;
  onDismiss: () => void;
};

export function SecretReveal({ label, secret, onDismiss }: SecretRevealProps) {
  const [copied, setCopied] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);

  async function copySecret() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <StatusBanner tone="warning" title="One-Time Secret">
      <p className="text-sm">
        {label}. Store it now. This value cannot be shown again after you dismiss this panel.
      </p>
      <code className="mt-3 block overflow-x-auto rounded-2xl border border-amber-200 bg-white/80 px-3 py-3 text-xs text-amber-950 dark:border-amber-900/40 dark:bg-slate-950 dark:text-amber-100">
        {secret}
      </code>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          className="rounded-2xl border border-amber-300 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] transition hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-900/30"
          onClick={copySecret}
          type="button"
        >
          {copied ? "Copied" : "Copy Secret"}
        </button>
        <label className="flex items-center gap-2 text-xs">
          <input checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} type="checkbox" />
          I stored this secret securely.
        </label>
        <button
          className="rounded-2xl bg-slate-900 px-3 py-2 text-xs font-semibold uppercase tracking-[0.16em] text-white transition hover:bg-slate-800 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
          disabled={!acknowledged}
          onClick={onDismiss}
          type="button"
        >
          Dismiss
        </button>
      </div>
    </StatusBanner>
  );
}
