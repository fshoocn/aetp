import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { aetpApi, type UserInfo } from "@/api/endpoints";

export const useAuthStore = defineStore("auth", () => {
  const user = ref<UserInfo | null>(
    JSON.parse(localStorage.getItem("user") || "null")
  );
  const token = ref<string | null>(localStorage.getItem("token"));

  const isLoggedIn = computed(() => !!token.value);

  async function login(username: string, password: string) {
    const data = await aetpApi.auth.login(username, password);
    token.value = data.access_token;
    localStorage.setItem("token", data.access_token);
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
    localStorage.setItem("user", JSON.stringify(u));
  }

  function logout() {
    token.value = null;
    user.value = null;
    localStorage.removeItem("token");
    localStorage.removeItem("user");
    window.location.hash = "#/login";
  }

  return { user, token, isLoggedIn, login, register, fetchMe, logout };
});
