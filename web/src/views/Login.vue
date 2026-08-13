<template>
  <div class="auth-page">
    <main class="auth-main">
      <div class="auth-brand"><span class="brand-mark">A</span><div><strong>AETP</strong><small>汽车电子测试平台</small></div></div>
      <div class="auth-topline"><el-tag effect="plain" type="success"><span class="status-dot"></span> 服务在线</el-tag><span>v0.1 workspace</span></div>
      <el-card class="auth-card" shadow="never">
        <div class="auth-heading">
          <span class="eyebrow">{{ isRegister ? "CREATE ACCESS" : "SECURE ACCESS" }}</span>
          <h2>{{ isRegister ? "申请工作区账户" : "进入测试控制台" }}</h2>
          <p>{{ isRegister ? "提交后由平台管理员审核，激活后即可加入项目。" : "使用平台账户进入项目运行工作区。" }}</p>
        </div>

        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="handleSubmit">
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" size="large" placeholder="输入用户名" :prefix-icon="User" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" size="large" type="password" show-password placeholder="输入密码" :prefix-icon="Lock" autocomplete="current-password" @keyup.enter="handleSubmit" />
          </el-form-item>
          <el-form-item v-if="isRegister" label="显示名称" prop="displayName">
            <el-input v-model="form.displayName" size="large" placeholder="用于工作区显示" :prefix-icon="EditPen" />
          </el-form-item>

          <el-alert v-if="message" :title="message" :type="messageType" :closable="false" show-icon class="form-alert" />
          <el-button native-type="submit" type="primary" size="large" class="submit" :loading="loading">
            {{ isRegister ? "提交注册申请" : "登录工作区" }}
            <el-icon class="submit-icon"><ArrowRight /></el-icon>
          </el-button>
        </el-form>

        <div class="auth-switch">
          <span>{{ isRegister ? "已经有账户？" : "还没有平台账户？" }}</span>
          <el-button text type="primary" @click="toggleMode">{{ isRegister ? "返回登录" : "申请账户" }}</el-button>
        </div>
      </el-card>
      <p class="auth-footnote"><el-icon><Lock /></el-icon> 访问受项目权限控制，账户状态和操作记录由平台统一管理。</p>
    </main>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { ElMessage } from "element-plus";
import { ArrowRight, EditPen, Lock, User } from "@element-plus/icons-vue";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();
const formRef = ref<FormInstance>();
const isRegister = ref(false);
const loading = ref(false);
const message = ref("");
const messageType = ref<"success" | "error" | "info">("error");
const form = reactive({ username: "", password: "", displayName: "" });

const rules: FormRules = {
  username: [
    { required: true, message: "请输入用户名", trigger: "blur" },
    { min: 3, max: 64, message: "用户名长度为 3-64 位", trigger: "blur" },
  ],
  password: [
    { required: true, message: "请输入密码", trigger: "blur" },
    { min: 6, max: 128, message: "密码长度为 6-128 位", trigger: "blur" },
  ],
  displayName: [{ required: true, message: "请输入显示名称", trigger: "blur" }],
};

function toggleMode() {
  isRegister.value = !isRegister.value;
  message.value = "";
  formRef.value?.clearValidate();
}

async function handleSubmit() {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  loading.value = true;
  message.value = "";
  try {
    if (isRegister.value) {
      await auth.register(form.username, form.password, form.displayName || form.username);
      messageType.value = "success";
      message.value = "申请已提交，请等待平台管理员审核。";
      isRegister.value = false;
      form.password = "";
    } else {
      await auth.login(form.username, form.password);
      ElMessage.success("登录成功");
      router.push("/dashboard");
    }
  } catch (error: unknown) {
    messageType.value = "error";
    message.value = error instanceof Error ? error.message : "操作失败，请稍后重试";
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.auth-page { display: grid; min-height: 100vh; place-items: center; padding: 32px 20px; background: #f5f7f9; }
.auth-main { display: flex; flex-direction: column; width: min(100%, 470px); }
.auth-brand { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 28px; color: #17212b; }
.brand-mark { display: grid; width: 38px; height: 38px; place-items: center; border-radius: 8px; background: #236bbd; color: #fff; font-size: 21px; font-weight: 800; }
.auth-brand div { display: flex; flex-direction: column; gap: 2px; }.auth-brand strong { font-size: 17px; letter-spacing: .14em; }.auth-brand small { color: #87949d; font-size: 11px; }
.eyebrow { color: #77a9d4; font-size: 10px; font-weight: 750; letter-spacing: .16em; }
.auth-topline { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; color: #87949d; font-size: 12px; }
.auth-topline { display: flex; align-items: center; justify-content: space-between; margin-bottom: 28px; color: #87949d; font-size: 12px; }
.auth-topline :deep(.el-tag) { border: 0; background: #e8f7f1; color: #278a64; }
.status-dot { display: inline-block; width: 6px; height: 6px; margin-right: 6px; border-radius: 50%; background: #39ad79; }
.auth-card { padding: 30px 32px 22px; border: 1px solid #e1e7eb; border-radius: 10px; background: #fff; }
.auth-heading { margin-bottom: 28px; }
.auth-heading h2 { margin: 9px 0 8px; color: #17212b; font-size: 27px; letter-spacing: -.02em; }
.auth-heading p { margin: 0; color: #7c8a94; font-size: 13px; line-height: 1.6; }
.auth-card :deep(.el-form-item__label) { padding-bottom: 7px; color: #4c5e6b; font-size: 12px; font-weight: 650; }
.auth-card :deep(.el-input__wrapper) { min-height: 44px; box-shadow: 0 0 0 1px #dfe6ec inset; }
.auth-card :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--aetp-blue) inset; }
.form-alert { margin: 6px 0 15px; }
.submit { width: 100%; height: 46px; margin-top: 5px; font-weight: 650; }
.submit-icon { margin-left: 8px; }
.auth-switch { display: flex; align-items: center; justify-content: center; margin-top: 18px; color: #83909a; font-size: 12px; }
.auth-switch .el-button { margin-left: 3px; }
.auth-footnote { display: flex; align-items: center; justify-content: center; gap: 5px; margin: 19px 0 0; color: #9aa6ae; font-size: 11px; }
@media (max-width: 520px) { .auth-page { padding: 20px 14px; } .auth-card { padding: 24px 20px 18px; } }
</style>
