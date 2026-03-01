export function SettingsOverviewPage() {
  return (
    <div className="workspace-section grid gap-4 xl:grid-cols-3">
      <section className="workspace-card">
        <h2 className="text-lg font-semibold">Implemented Controls</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
          <li>Sysadmin-gated user lifecycle management</li>
          <li>Project membership RBAC with global assignment tools</li>
          <li>Global API token create, update, rotate, and revoke</li>
          <li>Token scope catalog and role-default scope policy</li>
          <li>Global audit logs with paging and search</li>
          <li>Last-sysadmin and last-project-admin protection</li>
        </ul>
      </section>

      <section className="workspace-card">
        <h2 className="text-lg font-semibold">Operational Defaults</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
          <li>All privileged settings routes require sysadmin access</li>
          <li>API tokens respect scope checks and token role</li>
          <li>User self-lockout actions are blocked</li>
          <li>Settings UI aligned to IAM + tokens + audit governance sections</li>
          <li>Route smoke tests cover UI and API settings paths</li>
        </ul>
      </section>

      <section className="workspace-card">
        <h2 className="text-lg font-semibold">Enterprise Gaps To Plan</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
          <li>SSO and SCIM provisioning</li>
          <li>MFA and conditional access policies</li>
          <li>Fine-grained custom roles beyond project role tiers</li>
          <li>Session inventory and forced logout for individual sessions</li>
          <li>Audit log export and long-term retention controls</li>
        </ul>
      </section>
    </div>
  );
}
