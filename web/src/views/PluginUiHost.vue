<template>
  <div class="page">
    <header class="page-heading">
      <div>
        <span class="eyebrow">PLUGIN UI HOST</span>
        <h1>插件 UI</h1>
        <p>已启用 UI 插件以同源 iframe 加载；页面经 postMessage 与宿主通信。</p>
      </div>
      <div class="heading-actions">
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="errorMessage"
      title="插件 UI 清单加载失败"
      :description="errorMessage"
      type="error"
      show-icon
      :closable="false"
      class="page-alert"
    />

    <template v-if="uiPlugins.length">
      <section v-for="plugin in uiPlugins" :key="uiKey(plugin)" class="ui-card">
        <div class="ui-card-head">
          <div class="plugin-cell">
            <span class="plugin-mark"><el-icon><Monitor /></el-icon></span>
            <div>
              <strong>{{ plugin.plugin.manifest.display_name }}</strong>
              <small class="mono">{{ plugin.plugin.plugin_id }}@{{ plugin.plugin.version }}</small>
            </div>
          </div>
          <div class="ui-state">
            <span class="status-dot" :class="{ ok: plugin.ready }"></span>
            <span>{{ plugin.ready ? "已握手" : "等待握手" }}</span>
          </div>
        </div>

        <div v-if="plugin.configuration" class="ui-config">
          <span class="ui-config-label">最近 configuration.changed</span>
          <pre class="mono">{{ plugin.configuration }}</pre>
        </div>

        <iframe
          :ref="(el: unknown) => setIframe(plugin, el)"
          class="ui-frame"
          :src="uiSrc(plugin)"
          @load="onIframeLoad(plugin)"
        ></iframe>
      </section>
    </template>
    <el-card v-else-if="!loading" shadow="never" class="content-card">
      <el-empty description="暂无已启用的 UI 插件。请在插件中心安装并启用 point=ui 的插件。" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive } from "vue";
import { ElMessage } from "element-plus";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { Monitor, Refresh } from "@element-plus/icons-vue";
import { aetpApi, type PluginVersion } from "@/api/endpoints";
import { usePluginUiBridge, type PluginUiContext } from "@/composables/usePluginUiBridge";

interface UiPluginState {
  plugin: PluginVersion;
  ready: boolean;
  configuration: string | null;
}

const queryClient = useQueryClient();
const query = useQuery({
  queryKey: ["plugins", "governance"],
  queryFn: () => aetpApi.plugins.list(),
});
const plugins = computed(() => query.data.value ?? []);
const loading = computed(() => query.isLoading.value || query.isFetching.value);
const errorMessage = computed(() => query.error.value?.message || "");

function refresh() {
  queryClient.invalidateQueries({ queryKey: ["plugins"] });
}

// 稳定的每个插件握手/配置状态 + iframe/桥引用，跨查询刷新保持
const states: Record<string, UiPluginState> = {};
const iframeEls = new Map<string, HTMLIFrameElement>();
const bridgeByKey = new Map<string, ReturnType<typeof usePluginUiBridge>>();

function uiKey(state: UiPluginState): string {
  return `${state.plugin.plugin_id}@${state.plugin.version}`;
}

// 只托管「已启用 + 声明了 ui 入口」的 UI 插件
const uiPlugins = computed<UiPluginState[]>(() =>
  plugins.value
    .filter(
      (p) =>
        p.status === "enabled" &&
        p.manifest.point === "ui" &&
        Boolean(p.manifest.entrypoints?.ui),
    )
    .map((p) => {
      const key = `${p.plugin_id}@${p.version}`;
      const existing = states[key];
      if (existing) return existing;
      const created: UiPluginState = reactive({ plugin: p, ready: false, configuration: null });
      states[key] = created;
      return created;
    }),
);

function uiSrc(state: UiPluginState): string {
  // 由 Master 托管默认文档（entrypoints.ui）
  const plugin = state.plugin;
  return `/plugins/${encodeURIComponent(plugin.plugin_id)}/${encodeURIComponent(plugin.version)}/ui`;
}

function setIframe(state: UiPluginState, el: unknown): void {
  if (el) {
    iframeEls.set(uiKey(state), el as HTMLIFrameElement);
  }
}

function contextFor(state: UiPluginState): PluginUiContext {
  const plugin = state.plugin;
  return {
    plugin_id: plugin.plugin_id,
    version: plugin.version,
    point: plugin.manifest.point,
    display_name: plugin.manifest.display_name,
    ui_protocol_version: plugin.manifest.ui_protocol_version ?? null,
  };
}

function onIframeLoad(state: UiPluginState): void {
  const key = uiKey(state);
  const existing = bridgeByKey.get(key);
  if (existing) return;
  const frame = iframeEls.get(key);
  if (!frame) return;

  const bridge = usePluginUiBridge({
    iframe: { value: frame },
    context: contextFor(state),
    onReady: () => {
      state.ready = true;
    },
    onConfigurationChanged: (payload) => {
      const configuration = payload.configuration ?? {};
      state.configuration = JSON.stringify(configuration, null, 2);
      state.ready = true;
      ElMessage.success(`${state.plugin.manifest.display_name} 配置已更新（宿主收到 configuration.changed）`);
    },
  });
  bridgeByKey.set(key, bridge);
  // 握手：宿主告知插件当前上下文
  bridge.initialize();
}

onBeforeUnmount(() => {
  for (const bridge of bridgeByKey.values()) {
    bridge.destroy();
  }
  bridgeByKey.clear();
  iframeEls.clear();
});
</script>

<style scoped>
.page { max-width: 1280px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
.eyebrow { color: var(--aetp-cyan); font-size: 10px; font-weight: 800; letter-spacing: 0.16em; }
.page-heading h1 { margin: 8px 0 6px; color: var(--aetp-ink); font-size: 28px; }
.page-heading p { margin: 0; color: var(--aetp-muted); font-size: 13px; }
.heading-actions { display: flex; gap: 9px; }
.page-alert { margin-top: 14px; }
.ui-card {
  background: #fff;
  border: 1px solid var(--aetp-border, #e4e9f2);
  border-radius: 12px;
  padding: 16px 18px 14px;
  margin-bottom: 18px;
  box-shadow: 0 1px 2px rgba(16, 42, 67, 0.04);
}
.ui-card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.plugin-cell { display: flex; align-items: center; gap: 10px; }
.plugin-mark { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 7px; background: #eaf3ff; color: var(--aetp-blue); }
.plugin-cell div { display: flex; flex-direction: column; gap: 3px; }
.plugin-cell small { color: var(--aetp-muted); font: 12px ui-monospace, monospace; }
.ui-state { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--aetp-muted); }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #b7c2d0; }
.status-dot.ok { background: #34c759; }
.ui-config {
  background: #f6f8fb;
  border: 1px solid #eef2f7;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 10px;
}
.ui-config-label { font-size: 11px; color: var(--aetp-muted); }
.ui-config pre { margin: 6px 0 0; font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-all; }
.ui-frame {
  width: 100%;
  height: 480px;
  border: 1px solid var(--aetp-border, #e4e9f2);
  border-radius: 8px;
  background: #fff;
}
.mono { font: 12px ui-monospace, monospace; }
.content-card { border-radius: 12px; }
</style>
