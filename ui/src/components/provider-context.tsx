import type { ReactNode } from "react";

import {
  assessedIdentity,
  authModeLabel,
  collectionContextProvider,
  collectionCoverageText,
  collectionIsPartial,
  collectionLimitationLabel,
  collectionModeLabel,
  collectionSnapshotLabel,
  exposureDescription,
  exposureEvidenceSummary,
  exposureLabel,
  metadataBoolean,
  metadataString,
  normalizeExposure,
  normalizedProvider,
  providerLabel,
  type CollectionContext,
  type ProviderMetadata,
} from "@/lib/provider-context";

const PROVIDER_TONES: Record<string, string> = {
  sharepoint: "border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-800 dark:bg-sky-900/30 dark:text-sky-200",
  smb: "border-violet-300 bg-violet-50 text-violet-800 dark:border-violet-800 dark:bg-violet-900/30 dark:text-violet-200",
  nfs: "border-teal-300 bg-teal-50 text-teal-800 dark:border-teal-800 dark:bg-teal-900/30 dark:text-teal-200",
  network: "border-indigo-300 bg-indigo-50 text-indigo-800 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-200",
  unknown: "border-slate-300 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
};

const EXPOSURE_TONES: Record<string, string> = {
  USER_VISIBLE: "border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-800 dark:bg-sky-900/30 dark:text-sky-200",
  BROAD_INTERNAL: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-100",
  EXTERNAL: "border-orange-300 bg-orange-50 text-orange-900 dark:border-orange-800 dark:bg-orange-900/30 dark:text-orange-100",
  ANONYMOUS: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-900/30 dark:text-rose-100",
  RESTRICTED: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-100",
  UNKNOWN: "border-slate-300 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
};

const EXPOSURE_ICONS: Record<string, string> = {
  USER_VISIBLE: "◉",
  BROAD_INTERNAL: "◎",
  EXTERNAL: "↗",
  ANONYMOUS: "!",
  RESTRICTED: "✓",
  UNKNOWN: "?",
};

export function ProviderBadge({ provider, fallback }: { provider: unknown; fallback?: unknown }) {
  const normalized = normalizedProvider(provider, fallback);
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-semibold ${PROVIDER_TONES[normalized] || PROVIDER_TONES.unknown}`}>
      {providerLabel(normalized)}
    </span>
  );
}

export function ExposureBadge({ exposure, evidence, compact = false }: { exposure: unknown; evidence?: ProviderMetadata | null; compact?: boolean }) {
  const normalized = normalizeExposure(exposure);
  const evidenceSummary = exposureEvidenceSummary(evidence);
  return (
    <span
      aria-label={`Exposure: ${exposureLabel(normalized)}. ${exposureDescription(normalized)}${evidenceSummary ? ` Evidence: ${evidenceSummary}` : ""}`}
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-semibold ${compact ? "text-[10px]" : "text-[11px]"} ${EXPOSURE_TONES[normalized]}`}
      title={`${exposureDescription(normalized)}${evidenceSummary ? ` Evidence: ${evidenceSummary}` : ""}`}
    >
      <span aria-hidden="true" className="font-mono text-[10px]">{EXPOSURE_ICONS[normalized]}</span>
      {exposureLabel(normalized)}
    </span>
  );
}

function ContextFact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">{label}</dt>
      <dd className="mt-0.5 truncate text-xs font-semibold text-slate-800 dark:text-slate-100" title={typeof children === "string" ? children : undefined}>
        {children}
      </dd>
    </div>
  );
}

export function CollectionContextPanel({ context, compact = false }: { context: CollectionContext | null | undefined; compact?: boolean }) {
  if (!context || Object.keys(context).length === 0) {
    if (compact) return null;
    return (
      <section aria-label="Collection scope and identity" className="rounded-lg border border-slate-300 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/70">
        <h2 className="text-sm font-semibold">Collection scope unknown</h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
          This run does not contain provider, authentication, or completeness context. Treat coverage semantics as unknown; this is expected for artifacts created before context metadata was introduced.
        </p>
      </section>
    );
  }
  const provider = collectionContextProvider(context);
  const identity = assessedIdentity(context);
  const tenant = context.tenant_name || context.tenant_id;
  const partial = collectionIsPartial(context);
  const limitationLabel = collectionLimitationLabel(context);
  const isSharePoint = provider === "sharepoint";
  const snapshot = collectionSnapshotLabel(context);
  const failedContext = typeof context.status === "string" && ["failed", "error"].includes(context.status.trim().toLowerCase());
  const discoveryStrategy = metadataString(context.metadata, "discovery_strategy", "discoveryStrategy");
  const discoveryCompleteness = context.discovery_completeness || metadataString(context.metadata, "discovery_completeness", "discoveryCompleteness");
  const discoveryAuthoritative = metadataBoolean(context.metadata, "discovery_authoritative", "discoveryAuthoritative");
  const filesIncluded = metadataBoolean(context.metadata, "files_included", "filesIncluded");
  const permissionsAssessed = metadataBoolean(context.metadata, "permissions_assessed", "permissionsAssessed");
  const contentDownloaded = metadataBoolean(context.metadata, "content_downloaded", "contentDownloaded");
  const collectionMetadata = context.metadata?.collection && typeof context.metadata.collection === "object" && !Array.isArray(context.metadata.collection)
    ? context.metadata.collection as ProviderMetadata
    : null;
  const targetScope = collectionMetadata?.target_scope && typeof collectionMetadata.target_scope === "object" && !Array.isArray(collectionMetadata.target_scope)
    ? collectionMetadata.target_scope as ProviderMetadata
    : null;
  const targetedSites = Array.isArray(targetScope?.targeted_sites)
    ? targetScope.targeted_sites.filter((site): site is string => typeof site === "string" && !!site.trim())
    : [];
  const delegatedScopes = context.scopes?.filter((scope) => typeof scope === "string" && scope.trim()).join(", ") || null;
  const applicationRoles = context.roles?.filter((role) => typeof role === "string" && role.trim()).join(", ") || null;

  if (compact) {
    return (
      <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="Collection context">
        <ProviderBadge provider={provider} />
        <span className="rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 text-[11px] font-semibold text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200">
          {collectionModeLabel(context.collection_mode)}
        </span>
        {identity ? (
          <span className="max-w-full truncate text-xs text-slate-500" title={identity}>
            {context.collection_mode === "delegated_user_view" ? `Assessed user: ${identity}` : `Application identity: ${identity}`}
          </span>
        ) : null}
        {limitationLabel ? <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-100">{limitationLabel}</span> : null}
      </div>
    );
  }

  return (
    <section
      aria-label="Collection scope and identity"
      className={`rounded-lg border p-4 ${failedContext ? "border-rose-300 bg-rose-50/70 dark:border-rose-900/60 dark:bg-rose-900/15" : partial ? "border-amber-300 bg-amber-50/70 dark:border-amber-900/60 dark:bg-amber-900/15" : "border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/70"}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <ProviderBadge provider={provider} />
            <h2 className="text-sm font-semibold">{collectionModeLabel(context.collection_mode)}</h2>
            {failedContext ? <span className="rounded bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-900 dark:bg-rose-900/40 dark:text-rose-100">Context reports failure</span> : limitationLabel ? <span className="rounded bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-100">{limitationLabel}</span> : null}
          </div>
          <p className="mt-2 max-w-4xl text-sm text-slate-600 dark:text-slate-300">{collectionCoverageText(context)}</p>
        </div>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <ContextFact label="Authentication">{authModeLabel(context.auth_mode, context.auth_type)}</ContextFact>
        <ContextFact label={context.collection_mode === "delegated_user_view" ? "Assessed identity" : "Application identity"}>
          {identity || context.client_id || "Not recorded"}
        </ContextFact>
        <ContextFact label="Tenant">{tenant || "Not recorded"}</ContextFact>
        <ContextFact label="Coverage state">{failedContext ? "Collection error" : limitationLabel || context.status || (isSharePoint ? "Not reported" : "Recorded")}</ContextFact>
        {delegatedScopes ? <ContextFact label="Delegated Graph scopes">{delegatedScopes}</ContextFact> : null}
        {applicationRoles ? <ContextFact label="Application Graph roles">{applicationRoles}</ContextFact> : null}
        {context.client_id && context.client_id !== identity ? <ContextFact label="Application client ID">{context.client_id}</ContextFact> : null}
        {discoveryCompleteness ? <ContextFact label="Discovery completeness">{discoveryCompleteness.replaceAll("_", " ")}</ContextFact> : null}
        {snapshot ? <ContextFact label="Snapshot semantics">{snapshot}</ContextFact> : null}
        {discoveryStrategy ? <ContextFact label="Discovery strategy">{discoveryStrategy.replaceAll("_", " ")}</ContextFact> : null}
        {targetedSites.length > 0 ? <ContextFact label="Targeted sites">{targetedSites.join(", ")}</ContextFact> : null}
        {discoveryAuthoritative !== null ? (
          <ContextFact label="Discovery claim">
            {discoveryAuthoritative
              ? "Complete for granted scope"
              : context.collection_mode === "delegated_user_view"
                ? "Security-trimmed / non-authoritative"
                : "Non-authoritative / targeted scope"}
          </ContextFact>
        ) : null}
      </dl>
      {isSharePoint && (filesIncluded !== null || permissionsAssessed !== null || contentDownloaded !== null) ? (
        <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-slate-600 dark:text-slate-300" aria-label="SharePoint collection boundaries">
          {filesIncluded !== null ? <span className="rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-950">{filesIncluded ? "Filenames and folders included" : "Libraries only; files omitted"}</span> : null}
          {permissionsAssessed !== null ? <span className="rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-950">{permissionsAssessed ? "Permission evidence assessed" : "Permission expansion not assessed"}</span> : null}
          {contentDownloaded !== null ? <span className="rounded border border-slate-300 bg-white px-2 py-1 dark:border-slate-700 dark:bg-slate-950">{contentDownloaded ? "Content collection reported" : "Document content not downloaded"}</span> : null}
        </div>
      ) : null}
    </section>
  );
}
