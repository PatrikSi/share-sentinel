import { Component, ErrorInfo, ReactNode } from "react";

type AppErrorBoundaryProps = {
  children: ReactNode;
};

type AppErrorBoundaryState = {
  failed: boolean;
};

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Share Sentinel UI render failed", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;

    return (
      <main className="mx-auto flex min-h-screen max-w-xl items-center px-4 py-12">
        <section className="panel w-full text-center" role="alert">
          <p className="text-xs font-semibold uppercase tracking-wider text-rose-700 dark:text-rose-300">
            Interface error
          </p>
          <h1 className="mt-2 text-xl font-semibold">Share Sentinel could not render this page</h1>
          <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
            Refresh the application. If the problem continues, capture the browser console and run the deployment
            diagnostics on the server.
          </p>
          <button
            className="mt-5 rounded-md bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600"
            onClick={() => window.location.reload()}
            type="button"
          >
            Reload application
          </button>
        </section>
      </main>
    );
  }
}
