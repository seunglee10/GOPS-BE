import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type AuthUser = {
  email: string;
  name?: string | null;
  picture?: string | null;
};

type AuthContextValue = {
  authEnabled: boolean;
  user: AuthUser | null;
  loading: boolean;
  error?: string;
  login: () => void;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authEnabled, setAuthEnabled] = useState(false);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await fetch("/api/auth/me");
      if (!response.ok) {
        throw new Error(`인증 API 응답 오류 ${response.status}`);
      }
      const payload = await response.json() as unknown;
      const next = normalizeAuthPayload(payload);
      setAuthEnabled(next.authEnabled);
      setUser(next.user);
    } catch (caught) {
      setAuthEnabled(true);
      setUser(null);
      setError(caught instanceof Error ? caught.message : "인증 상태를 확인하지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(() => {
    const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.location.href = `/api/auth/google/login?returnTo=${encodeURIComponent(returnTo)}`;
  }, []);

  const logout = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await fetch("/api/auth/logout", { method: "POST" });
      if (!response.ok && response.status !== 204) {
        throw new Error(`로그아웃 API 응답 오류 ${response.status}`);
      }
      setUser(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "로그아웃에 실패했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    authEnabled,
    user,
    loading,
    error,
    login,
    logout,
    refresh
  }), [authEnabled, error, loading, login, logout, refresh, user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}

function normalizeAuthPayload(payload: unknown): { authEnabled: boolean; user: AuthUser | null } {
  if (!payload || typeof payload !== "object") {
    return { authEnabled: false, user: null };
  }
  const source = payload as Record<string, unknown>;
  return {
    authEnabled: source.authEnabled === true,
    user: normalizeUser(source.user)
  };
}

function normalizeUser(value: unknown): AuthUser | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const source = value as Record<string, unknown>;
  if (typeof source.email !== "string" || !source.email.trim()) {
    return null;
  }
  return {
    email: source.email,
    name: typeof source.name === "string" ? source.name : null,
    picture: typeof source.picture === "string" ? source.picture : null
  };
}
