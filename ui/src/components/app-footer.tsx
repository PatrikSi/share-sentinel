import { version } from "../../package.json";

export function AppFooter() {
  return (
    <footer aria-label={`Share Sentinel application version ${version}`} className="app-footer" data-app-version={version}>
      <div className="app-footer-inner" title={`Share Sentinel version ${version}`}>Share Sentinel v{version}</div>
    </footer>
  );
}
