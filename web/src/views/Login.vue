<template>
  <div class="login-wrapper">
    <el-card class="login-card">
      <h1 class="title">AETP</h1>
      <p class="sub">汽车电子测试平台</p>

      <el-form @submit.prevent="handleLogin">
        <el-form-item>
          <el-input
            v-model="form.username"
            placeholder="用户名"
            size="large"
            :prefix-icon="User"
          />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            size="large"
            show-password
            :prefix-icon="Lock"
            @keyup.enter="handleLogin"
          />
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          class="submit"
          :loading="loading"
          @click="handleLogin"
        >
          {{ isRegister ? "注 册" : "登 录" }}
        </el-button>

        <div class="toggle">
          {{ isRegister ? "已有账号？" : "没有账号？" }}
          <el-link type="primary" @click="isRegister = !isRegister">
            {{ isRegister ? "去登录" : "去注册" }}
          </el-link>
        </div>

        <el-alert
          v-if="message"
          :title="message"
          :type="messageType"
          :closable="false"
          show-icon
        />
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { User, Lock } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();

const form = reactive({ username: "", password: "" });
const isRegister = ref(false);
const loading = ref(false);
const message = ref("");
const messageType = ref<"success" | "error" | "info">("error");

async function handleLogin() {
  message.value = "";
  if (form.username.length < 3 || form.password.length < 6) {
    message.value = "用户名至少 3 位，密码至少 6 位";
    messageType.value = "error";
    return;
  }
  loading.value = true;
  try {
    if (isRegister.value) {
      await auth.register(form.username, form.password, form.username);
      message.value = "注册成功，请等待管理员审批后登录";
      messageType.value = "success";
      isRegister.value = false;
    } else {
      await auth.login(form.username, form.password);
      ElMessage.success("登录成功");
      router.push("/dashboard");
    }
  } catch (e: unknown) {
    message.value = e instanceof Error ? e.message : "操作失败";
    messageType.value = "error";
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.login-wrapper {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.login-card {
  width: 380px;
  padding: 24px 16px 8px;
}
.title {
  text-align: center;
  margin: 0;
  font-size: 28px;
  color: #1a73e8;
}
.sub {
  text-align: center;
  color: #666;
  margin: 8px 0 20px;
}
.submit {
  width: 100%;
}
.toggle {
  text-align: center;
  margin: 12px 0;
  color: #666;
  font-size: 13px;
}
</style>
