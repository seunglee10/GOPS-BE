import { Component, type ErrorInfo, type ReactNode } from "react";

type AppErrorBoundaryProps = {
  children: ReactNode;
};

type AppErrorBoundaryState = {
  error?: Error;
};

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = {};

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("GOPS frontend render error", { error, componentStack: info.componentStack });
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <main className="app-error-boundary">
        <section className="app-error-panel">
          <p className="app-error-kicker">Frontend render error</p>
          <h1>GOPS 화면을 복구하지 못했습니다.</h1>
          <p>
            화면이 하얗게 사라지지 않도록 오류를 잡았습니다. 개발자 콘솔에서 원인을 확인한 뒤
            새로고침해 주세요.
          </p>
          <pre>{this.state.error.message}</pre>
          <button type="button" onClick={() => window.location.reload()}>
            Reload
          </button>
        </section>
      </main>
    );
  }
}
