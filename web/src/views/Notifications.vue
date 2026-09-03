<template>
  <div class="notifications-page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">NOTIFICATIONS / PROJECT SETTINGS</span>
        <h1>通知与订阅</h1>
        <p>先配置通知端点，再把指定测试任务的进度或结果绑定到通知通道。密钥永不回显。</p>
      </div>
      <div class="heading-actions">
        <el-button v-if="isOwner" type="primary" :icon="Plus" @click="openEndpointCreate">新建端点</el-button>
        <el-button v-if="canManageSubscription" :icon="Connection" :disabled="endpoints.length === 0" @click="openSubscriptionCreate">新建任务订阅</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="!projectId" title="尚未选择项目" description="请先从顶部选择一个项目。" type="info" show-icon :closable="false" />

    <!-- 通知端点 -->
    <el-card v-if="projectId" class="section-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><span class="section-kicker">ENDPOINTS</span><strong>通知端点</strong></div>
          <el-tag effect="light">{{ endpoints.length }} 个</el-tag>
        </div>
      </template>
      <el-table :data="endpoints" row-key="endpoint_id" v-loading="loading">
        <el-table-column label="名称" min-width="160" prop="name" />
        <el-table-column label="通道" width="140">
          <template #default="{ row }"><el-tag effect="plain" size="small">{{ row.channel_type }}</el-tag></template>
        </el-table-column>
        <el-table-column label="密钥" width="100">
          <template #default="{ row }">
            <el-tag :type="row.has_secret ? 'success' : 'info'" effect="light" size="small">
              {{ row.has_secret ? '已配置' : '无' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" effect="light" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ row.created_at ? fmt(row.created_at) : '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button v-if="isOwner" link type="warning" @click.stop="openEndpointEdit(row)">编辑</el-button>
            <el-button v-if="isOwner" link :type="row.enabled ? 'info' : 'success'" @click.stop="toggleEndpoint(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button v-if="isOwner" link type="danger" @click.stop="deleteEndpoint(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && endpoints.length === 0" description="尚未创建通知端点" />
    </el-card>

    <!-- 事件订阅 -->
    <el-card v-if="projectId" class="section-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><span class="section-kicker">TASK SUBSCRIPTIONS</span><strong>测试任务通知</strong></div>
          <el-tag effect="light">{{ subscriptions.length }} 个</el-tag>
        </div>
      </template>
      <el-table :data="subscriptions" row-key="subscription_id" v-loading="loading">
        <el-table-column label="订阅范围" min-width="190">
          <template #default="{ row }">
            <div class="subscription-scope">
              <strong>{{ row.task_id ? taskName(row.task_id) : '全部测试任务' }}</strong>
              <small>{{ row.task_id || '项目级事件' }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="端点" min-width="160">
          <template #default="{ row }">{{ endpointName(row.endpoint_id) }}</template>
        </el-table-column>
        <el-table-column label="事件类型" min-width="240">
          <template #default="{ row }">
            <el-tag v-for="et in row.event_types" :key="et" size="small" effect="plain" class="event-tag">{{ et }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" effect="light" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button v-if="canManageSubscription" link type="warning" @click.stop="openSubscriptionEdit(row)">编辑</el-button>
            <el-button v-if="canManageSubscription" link :type="row.enabled ? 'info' : 'success'" @click.stop="toggleSubscription(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button v-if="canManageSubscription" link type="danger" @click.stop="deleteSubscription(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && subscriptions.length === 0" description="尚未创建事件订阅" />
    </el-card>

    <!-- 投递状态 -->
    <el-card v-if="projectId" class="section-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><span class="section-kicker">DELIVERIES</span><strong>投递记录</strong></div>
          <el-select v-model="deliveryFilter" size="small" style="width: 140px" @change="refreshDeliveries" clearable placeholder="全部状态">
            <el-option label="待发送" value="pending" />
            <el-option label="成功" value="succeeded" />
            <el-option label="重试中" value="retrying" />
            <el-option label="已耗尽" value="exhausted" />
          </el-select>
        </div>
      </template>
      <el-table :data="deliveries" row-key="delivery_id" v-loading="deliveryLoading">
        <el-table-column type="expand" width="46">
          <template #default="{ row }">
            <div class="delivery-content">
              <div class="delivery-content-head"><strong>实际投递内容</strong><el-tag size="small" effect="plain">{{ row.content?.event_type || '未知事件' }}</el-tag></div>
              <pre>{{ prettyContent(row.content) }}</pre>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="投递 ID" width="200">
          <template #default="{ row }"><span class="mono">{{ row.delivery_id }}</span></template>
        </el-table-column>
        <el-table-column label="事件 ID" width="200">
          <template #default="{ row }"><span class="mono">{{ row.event_id }}</span></template>
        </el-table-column>
        <el-table-column label="端点" min-width="140">
          <template #default="{ row }">{{ endpointName(row.endpoint_id) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="deliveryTagType(row.status)" effect="light" size="small">{{ deliveryText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="70" prop="attempts" />
        <el-table-column label="错误" min-width="200">
          <template #default="{ row }">{{ row.error_message || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button v-if="isOwner && canRetry(row.status)" link type="primary" @click.stop="retryDelivery(row)">重试</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!deliveryLoading && deliveries.length === 0" description="暂无投递记录" />
    </el-card>

    <!-- 端点编辑弹窗 -->
    <el-dialog v-model="endpointDialogVisible" :title="editingEndpoint ? '编辑通知端点' : '新建通知端点'" width="560px" destroy-on-close>
      <el-form :model="endpointForm" label-position="top">
        <el-form-item label="名称" required>
          <el-input v-model="endpointForm.name" placeholder="如 生产告警 / 飞书机器人" />
        </el-form-item>
        <el-form-item label="通道类型" required>
          <el-select v-model="endpointForm.channel_type" style="width: 100%">
            <el-option label="通用 Webhook" value="generic_webhook" />
            <el-option label="飞书" value="feishu" />
            <el-option label="钉钉" value="dingtalk" />
            <el-option label="Slack" value="slack" />
            <el-option label="Teams" value="teams" />
            <el-option label="邮件" value="email" />
            <el-option label="控制台测试" value="console_test" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="endpointForm.channel_type === 'generic_webhook'" label="Webhook URL">
          <el-input v-model="endpointForm.webhookUrl" placeholder="https://example.com/webhook" />
        </el-form-item>
        <el-form-item label="密钥（可选，仅保存引用，不回显）">
          <el-input v-model="endpointForm.secret_value" type="password" show-password placeholder="Webhook Token / API Key 等" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="endpointDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="endpointSaving" @click="saveEndpoint">保存</el-button>
      </template>
    </el-dialog>

    <!-- 订阅编辑弹窗 -->
    <el-dialog v-model="subscriptionDialogVisible" :title="editingSubscription ? '编辑任务通知' : '新建任务通知'" width="640px" destroy-on-close>
      <el-form :model="subscriptionForm" label-position="top">
        <el-alert title="把指定测试任务的运行进度、用例状态或最终结果推送到已配置的通知端点。" type="info" show-icon :closable="false" class="subscription-help" />
        <el-form-item label="通知端点" required>
          <el-select v-model="subscriptionForm.endpoint_id" style="width: 100%" placeholder="选择端点">
            <el-option v-for="ep in endpoints" :key="ep.endpoint_id" :label="`${ep.name} (${ep.channel_type})`" :value="ep.endpoint_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="测试任务">
          <el-select v-model="subscriptionForm.task_id" style="width: 100%" placeholder="全部测试任务（项目级）" clearable>
            <el-option label="全部测试任务（项目级）" value="" />
            <el-option v-for="task in testTasks" :key="task.task.task_id" :label="`${task.task.name} · revision ${task.task.revision}`" :value="task.task.task_id" />
          </el-select>
          <div class="form-hint">选择具体任务后，只有该任务产生的事件会触发通知。</div>
        </el-form-item>
        <el-form-item label="事件类型" required>
          <el-select v-model="subscriptionForm.event_types" multiple filterable allow-create style="width: 100%" placeholder="选择或输入事件类型">
            <el-option label="运行进度" value="run.progress" />
            <el-option label="用例状态" value="run.case-status" />
            <el-option label="运行结果" value="run.result" />
            <el-option label="日志完成" value="run.log_complete" />
            <el-option label="开始派发" value="run.dispatched" />
            <el-option label="派发失败" value="run.failed" />
          </el-select>
          <div class="form-hint">推荐选择“运行进度 + 运行结果”，分别获得实时进度和最终结果。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="subscriptionDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="subscriptionSaving" @click="saveSubscription">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import type { FormInstance } from "element-plus";
import { ElMessage, ElMessageBox } from "element-plus";
import { Connection, Plus, Refresh } from "@element-plus/icons-vue";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { aetpApi, type NotificationEndpointOut, type EventSubscriptionOut, type EventDeliveryOut } from "@/api/endpoints";
import type { TaskView } from "@/api/types";
import { useAuthStore } from "@/stores/auth";
import { useProjectStore } from "@/stores/project";

const auth = useAuthStore();
const projectStore = useProjectStore();
const qc = useQueryClient();

const projectId = computed(() => projectStore.currentProjectId ?? "");
const canManageSubscription = computed(() => auth.user?.platform_role === "admin" || ["operator", "maintainer", "owner"].includes(projectStore.currentRole || ""));
const isOwner = computed(() => auth.user?.platform_role === "admin" || projectStore.currentRole === "owner");

// ---- 数据 ----
const endpointsQuery = useQuery({
  queryKey: ["notificationEndpoints", projectId],
  queryFn: () => aetpApi.notifications.listEndpoints(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const subscriptionsQuery = useQuery({
  queryKey: ["eventSubscriptions", projectId],
  queryFn: () => aetpApi.notifications.listSubscriptions(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const testTasksQuery = useQuery({
  queryKey: ["testTasks", "notifications", projectId],
  queryFn: () => aetpApi.tasks.listTasks(projectId.value),
  enabled: computed(() => !!projectId.value),
});
const deliveryFilter = ref<string | undefined>(undefined);
const deliveriesQuery = useQuery({
  queryKey: ["eventDeliveries", projectId, deliveryFilter],
  queryFn: () => aetpApi.notifications.listDeliveries(projectId.value, deliveryFilter.value),
  enabled: computed(() => !!projectId.value),
});
const endpoints = computed(() => endpointsQuery.data.value ?? []);
const subscriptions = computed(() => subscriptionsQuery.data.value ?? []);
const testTasks = computed<TaskView[]>(() => testTasksQuery.data.value ?? []);
const deliveries = computed(() => deliveriesQuery.data.value ?? []);
const loading = computed(() => endpointsQuery.isLoading.value || subscriptionsQuery.isLoading.value);
const deliveryLoading = computed(() => deliveriesQuery.isLoading.value);

function refresh() {
  qc.invalidateQueries({ queryKey: ["notificationEndpoints"] });
  qc.invalidateQueries({ queryKey: ["eventSubscriptions"] });
  qc.invalidateQueries({ queryKey: ["eventDeliveries"] });
}
function refreshDeliveries() {
  qc.invalidateQueries({ queryKey: ["eventDeliveries"] });
}

function endpointName(id: string) {
  return endpoints.value.find((ep) => ep.endpoint_id === id)?.name || id;
}
function taskName(id: string) {
  return testTasks.value.find((task) => task.task.task_id === id)?.task.name || id;
}

// ---- 端点 ----
const endpointDialogVisible = ref(false);
const editingEndpoint = ref<NotificationEndpointOut | null>(null);
const endpointSaving = ref(false);
const endpointForm = reactive({ name: "", channel_type: "generic_webhook", webhookUrl: "", secret_value: "" });

function openEndpointCreate() {
  editingEndpoint.value = null;
  endpointForm.name = ""; endpointForm.channel_type = "generic_webhook"; endpointForm.webhookUrl = ""; endpointForm.secret_value = "";
  endpointDialogVisible.value = true;
}
function openEndpointEdit(ep: NotificationEndpointOut) {
  editingEndpoint.value = ep;
  endpointForm.name = ep.name; endpointForm.channel_type = ep.channel_type;
  endpointForm.webhookUrl = (ep.config as Record<string, unknown>)?.url as string || "";
  endpointForm.secret_value = "";
  endpointDialogVisible.value = true;
}
async function saveEndpoint() {
  if (!endpointForm.name.trim()) { ElMessage.warning("请填写端点名称"); return; }
  endpointSaving.value = true;
  try {
    const config: Record<string, unknown> = editingEndpoint.value
      ? { ...(editingEndpoint.value.config as Record<string, unknown>) }
      : {};
    if (endpointForm.channel_type === "generic_webhook" && endpointForm.webhookUrl) config.url = endpointForm.webhookUrl;
    if (editingEndpoint.value) {
      await aetpApi.notifications.updateEndpoint(projectId.value, editingEndpoint.value.endpoint_id, {
        name: endpointForm.name, config, secret_value: endpointForm.secret_value || undefined,
      });
      ElMessage.success("端点已更新");
    } else {
      await aetpApi.notifications.createEndpoint(projectId.value, {
        channel_type: endpointForm.channel_type, name: endpointForm.name, config, secret_value: endpointForm.secret_value || undefined,
      });
      ElMessage.success("端点已创建");
    }
    endpointDialogVisible.value = false;
    refresh();
  } catch (e) { ElMessage.error((e as Error).message); } finally { endpointSaving.value = false; }
}
const toggleEndpointMut = useMutation({
  mutationFn: (ep: NotificationEndpointOut) => aetpApi.notifications.updateEndpoint(projectId.value, ep.endpoint_id, { enabled: !ep.enabled }),
  onSuccess: () => { ElMessage.success("端点状态已变更"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function toggleEndpoint(ep: NotificationEndpointOut) {
  toggleEndpointMut.mutate(ep);
}
async function deleteEndpoint(ep: NotificationEndpointOut) {
  try { await ElMessageBox.confirm(`确认删除端点「${ep.name}」？关联的订阅将被级联删除。`, "删除端点", { type: "warning" }); } catch { return; }
  try {
    await aetpApi.notifications.deleteEndpoint(projectId.value, ep.endpoint_id);
    ElMessage.success("端点已删除");
    refresh();
  } catch (e) { ElMessage.error((e as Error).message); }
}

// ---- 订阅 ----
const subscriptionDialogVisible = ref(false);
const editingSubscription = ref<EventSubscriptionOut | null>(null);
const subscriptionSaving = ref(false);
const subscriptionForm = reactive({ endpoint_id: "", task_id: "", event_types: [] as string[] });

function openSubscriptionCreate() {
  editingSubscription.value = null;
  subscriptionForm.endpoint_id = ""; subscriptionForm.task_id = ""; subscriptionForm.event_types = [];
  subscriptionDialogVisible.value = true;
}
function openSubscriptionEdit(sub: EventSubscriptionOut) {
  editingSubscription.value = sub;
  subscriptionForm.endpoint_id = sub.endpoint_id; subscriptionForm.task_id = sub.task_id || ""; subscriptionForm.event_types = [...sub.event_types];
  subscriptionDialogVisible.value = true;
}
async function saveSubscription() {
  if (!subscriptionForm.endpoint_id) { ElMessage.warning("请选择端点"); return; }
  if (!subscriptionForm.event_types.length) { ElMessage.warning("请选择至少一个事件类型"); return; }
  subscriptionSaving.value = true;
  try {
    if (editingSubscription.value) {
      await aetpApi.notifications.updateSubscription(projectId.value, editingSubscription.value.subscription_id, {
        task_id: subscriptionForm.task_id,
        event_types: subscriptionForm.event_types,
      });
      ElMessage.success("订阅已更新");
    } else {
      await aetpApi.notifications.createSubscription(projectId.value, {
        endpoint_id: subscriptionForm.endpoint_id,
        ...(subscriptionForm.task_id ? { task_id: subscriptionForm.task_id } : {}),
        event_types: subscriptionForm.event_types,
      });
      ElMessage.success("订阅已创建");
    }
    subscriptionDialogVisible.value = false;
    refresh();
  } catch (e) { ElMessage.error((e as Error).message); } finally { subscriptionSaving.value = false; }
}
const toggleSubMut = useMutation({
  mutationFn: (sub: EventSubscriptionOut) => aetpApi.notifications.updateSubscription(projectId.value, sub.subscription_id, { enabled: !sub.enabled }),
  onSuccess: () => { ElMessage.success("订阅状态已变更"); refresh(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function toggleSubscription(sub: EventSubscriptionOut) {
  toggleSubMut.mutate(sub);
}
async function deleteSubscription(sub: EventSubscriptionOut) {
  try { await ElMessageBox.confirm("确认删除此事件订阅？", "删除订阅", { type: "warning" }); } catch { return; }
  try {
    await aetpApi.notifications.deleteSubscription(projectId.value, sub.subscription_id);
    ElMessage.success("订阅已删除");
    refresh();
  } catch (e) { ElMessage.error((e as Error).message); }
}

// ---- 投递 ----
function canRetry(status: string) { return status === "exhausted" || status === "failed"; }
function deliveryText(status: string) {
  return ({ pending: "待发送", sending: "发送中", succeeded: "成功", retrying: "重试中", exhausted: "已耗尽", cancelled: "已取消" } as Record<string, string>)[status] || status;
}
function deliveryTagType(status: string) {
  return ({ succeeded: "success", pending: "info", sending: "warning", retrying: "warning", exhausted: "danger", failed: "danger", cancelled: "info" } as Record<string, "success" | "danger" | "warning" | "info">)[status] || "info";
}
const retryMut = useMutation({
  mutationFn: (d: EventDeliveryOut) => aetpApi.notifications.retryDelivery(projectId.value, d.delivery_id),
  onSuccess: () => { ElMessage.success("已重新入队"); refreshDeliveries(); },
  onError: (e: Error) => ElMessage.error(e.message),
});
async function retryDelivery(d: EventDeliveryOut) {
  try { await ElMessageBox.confirm("确认重试此投递？", "重试投递", { type: "warning" }); } catch { return; }
  retryMut.mutate(d);
}

function fmt(v: string) { return new Date(v).toLocaleString("zh-CN", { hour12: false }); }
function prettyContent(value: Record<string, unknown>) { return JSON.stringify(value || {}, null, 2); }
</script>

<style scoped>
.notifications-page { max-width: 1480px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.page-heading h1 { margin: 8px 0 6px; font-size: 28px; }
.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }
.heading-actions { display: flex; align-items: center; gap: 10px; }
.section-card { border: 1px solid var(--aetp-line); margin-bottom: 16px; }
.section-card :deep(.el-card__header) { padding: 17px 20px; border-bottom-color: var(--aetp-line); }
.section-card :deep(.el-card__body) { padding: 0; }
.card-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.card-heading > div { display: flex; flex-direction: column; gap: 5px; }
.card-heading strong { color: var(--aetp-ink); font-size: 15px; }
.section-kicker { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: .16em; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.event-tag { margin-right: 4px; }
.subscription-scope { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.subscription-scope strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.subscription-scope small, .form-hint { color: var(--aetp-muted); font-size: 11px; }
.subscription-help { margin-bottom: 18px; }
.delivery-content { margin: -4px 0; padding: 14px 24px 14px 48px; background: #f7fafb; }
.delivery-content-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; color: var(--aetp-ink); font-size: 12px; }
.delivery-content pre { max-height: 240px; margin: 0; overflow: auto; padding: 12px; border: 1px solid var(--aetp-line); border-radius: 6px; background: #172b35; color: #c8e6e5; font: 11px/1.55 ui-monospace, monospace; white-space: pre-wrap; }
@media (max-width: 760px) {
  .page-heading { align-items: flex-start; flex-direction: column; gap: 14px; }
  .heading-actions { width: 100%; justify-content: space-between; }
}
</style>
