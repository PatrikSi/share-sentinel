import { version } from "../../package.json";

export function AppFooter() {
  return (
    <footer aria-label="Application version" className="app-footer">
      <div className="app-footer-inner">Share Sentinel v{version}</div>
    </footer>
  );
}
