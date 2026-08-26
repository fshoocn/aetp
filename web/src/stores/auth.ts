import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { aetpApi, type LoginResponse, type UserInfo } from "@/api/endpoints";

const ACCESS_KEY = "token";
const REFRESH_KEY = "refresh_token";
const USER_KEY = "user";

export function readStoredTokens(): {
  access: string | null;
  refresh: string | null;
} {
  return {
    access: localStorage.getItem(ACCESS_KEY),
    refresh: localStorage.getItem(REFRESH_KEY),
  };
}

export function persistTokens(data: LoginResponse): void {
  localStorage.setItem(ACCESS_KEY, data.access_token);
  localStorage.setItem(REFRESH_KEY, data.refresh_token);
}

export function clearStoredTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(USER_KEY);
}

export const useAuthStore = defineStore("auth", () => {
  let storedUser: UserInfo | null = null;
  try {
    storedUser = JSON.parse(localStorage.getItem(USER_KEY) || "null") as UserInfo | null;
  } catch {
    localStorage.removeItem(USER_KEY);
  }
  const user = ref<UserInfo | null>(storedUser);
  const token = ref<string | null>(localStorage.getItem(ACCESS_KEY));
  const refreshToken = ref<string | null>(localStorage.getItem(REFRESH_KEY));

  const isLoggedIn = computed(() => !!token.value);

  function applyTokens(data: LoginResponse) {
    token.value = data.access_token;
    refreshToken.value = data.refresh_token;
    persistTokens(data);
  }

  async function login(username: string, password: string) {
    const data = await aetpApi.auth.login(username, password);
    applyTokens(data);
    await fetchMe();
  }

  async function register(
    username: string,
    password: string,
    displayName: string
  ) {
    await aetpApi.auth.register(username, password, displayName);
  }

  async function fetchMe() {
    const u = await aetpApi.auth.me();
    user.value = u;
    localStorage.setItem(USER_KEY, JSON.stringify(u));
  }

  /** 静默续期：成功返回 true 并更新本地令牌。 */
  async function tryRefresh(): Promise<boolean> {
    if (!refreshToken.value) return false;
    try {
      const data = await aetpApi.auth.refresh(refreshToken.value);
      applyTokens(data);
      return true;
    } catch {
      return false;
    }
  }

  async function logout() {
    const rt = refreshToken.value;
    clearStoredTokens();
    token.value = null;
    refreshToken.value = null;
    user.value = null;
    // 尽力通知服务端撤销刷新令牌（失败不影响本地登出）
    if (rt) {
      try {
        await aetpApi.auth.logout(rt);
      } catch {
        /* ignore */
      }
    }
    window.location.hash = "#/login";
  }

  return {
    user,
    token,
    refreshToken,
    isLoggedIn,
    login,
    register,
    fetchMe,
    tryRefresh,
    logout,
  };
});
