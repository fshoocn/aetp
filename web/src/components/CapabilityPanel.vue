<template>
  <div class="capability-panel">
    <el-empty v-if="!hasAny" description="节点未上报能力（Agent 启动自动扫描）" :image-size="64" />

    <!-- vehicle：厂商 → 总线 → 通道 -->
    <div v-if="vehicle?.vendors?.length" class="cap-section">
      <div class="cap-section-title"><el-icon class="title-icon"><Cpu /></el-icon>车载总线</div>
      <div v-for="vendor in vehicle.vendors" :key="vendor.name" class="cap-vendor">
        <div class="cap-vendor-name">{{ vendor.name }}</div>
        <div v-for="bus in vendor.buses" :key="bus.bus_type" class="cap-bus">
          <div class="cap-bus-type"><el-tag size="small" effect="plain">{{ bus.bus_type.toUpperCase() }}</el-tag></div>
          <div class="cap-channels">
            <div v-for="ch in bus.channels" :key="ch.name" class="cap-channel" :class="{ disabled: ch.enabled === false }">
              <el-icon :class="ch.enabled === false ? 'off' : 'on'"><CircleCheck v-if="ch.enabled !== false" /><CircleClose v-else /></el-icon>
              <span class="ch-name">{{ ch.name }}</span>
              <span v-if="ch.hardware_model" class="ch-model">{{ ch.hardware_model }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- language：运行时 + 版本 -->
    <div v-if="language?.runtimes?.length" class="cap-section">
      <div class="cap-section-title"><el-icon class="title-icon"><Monitor /></el-icon>语言运行时</div>
      <div class="cap-tags">
        <el-tag v-for="r in language.runtimes" :key="r.name" size="small" type="info" effect="plain">{{ r.name }} <span class="mono">{{ r.version }}</span></el-tag>
      </div>
    </div>

    <!-- system：OS / 内存 / CPU -->
    <div v-if="system" class="cap-section">
      <div class="cap-section-title"><el-icon class="title-icon"><SetUp /></el-icon>系统资源</div>
      <div class="cap-tags">
        <el-tag v-if="system.operating_system" size="small" type="info" effect="plain">{{ system.operating_system.name }} <span class="mono">{{ system.operating_system.version }}</span></el-tag>
        <el-tag v-if="system.memory_mb != null" size="small" type="info" effect="plain">内存 <span class="mono">{{ formatMem(system.memory_mb) }}</span></el-tag>
        <el-tag v-if="system.cpu_cores != null" size="small" type="info" effect="plain">CPU <span class="mono">{{ system.cpu_cores }} 核</span></el-tag>
      </div>
    </div>

    <!-- serial：功能 → 端口 -->
    <div v-if="serial?.ports?.length" class="cap-section">
      <div class="cap-section-title"><el-icon class="title-icon"><Connection /></el-icon>串口资源</div>
      <div class="cap-ports">
        <div v-for="p in serial.ports" :key="p.function" class="cap-port" :class="{ disabled: p.enabled === false }">
          <el-icon :class="p.enabled === false ? 'off' : 'on'"><CircleCheck v-if="p.enabled !== false" /><CircleClose v-else /></el-icon>
          <span class="port-func">{{ p.function }}</span>
          <span class="mono">{{ p.port }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { CircleCheck, CircleClose, Connection, Cpu, Monitor, SetUp } from "@element-plus/icons-vue";
import type { NodeCapabilities } from "@/api/endpoints";

const props = defineProps<{ capabilities?: NodeCapabilities | null }>();

const caps = computed(() => props.capabilities ?? null);
const vehicle = computed(() => caps.value?.vehicle ?? null);
const language = computed(() => caps.value?.language ?? null);
const system = computed(() => caps.value?.system ?? null);
const serial = computed(() => caps.value?.serial ?? null);

const hasAny = computed(
  () =>
    !!(vehicle.value?.vendors?.length) ||
    !!(language.value?.runtimes?.length) ||
    (system.value != null && (system.value.operating_system != null || system.value.memory_mb != null || system.value.cpu_cores != null)) ||
    !!(serial.value?.ports?.length),
);

function formatMem(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}
</script>

<style scoped>
.capability-panel { padding: 6px 2px; }
.cap-section { margin-bottom: 16px; }
.cap-section:last-child { margin-bottom: 0; }
.cap-section-title { display: flex; align-items: center; gap: 7px; color: #42566a; font-size: 13px; font-weight: 650; margin-bottom: 9px; }
.title-icon { color: var(--aetp-blue); }
.cap-vendor { margin-bottom: 8px; }
.cap-vendor-name { color: #789096; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 5px; }
.cap-bus { margin-bottom: 7px; }
.cap-bus-type { margin-bottom: 5px; }
.cap-channels { display: flex; flex-wrap: wrap; gap: 6px; }
.cap-channel { display: inline-flex; align-items: center; gap: 6px; background: #f7f9fb; border: 1px solid #edf1f4; border-radius: 6px; padding: 5px 10px; font-size: 12px; }
.cap-channel .on, .cap-port .on { color: #2f9d71; }
.cap-channel .off, .cap-port .off { color: #c45656; }
.cap-channel.disabled, .cap-port.disabled { opacity: .6; background: #fbf4f4; }
.ch-name { color: #2c3e50; font-weight: 600; }
.ch-model { color: #96a3ac; font-size: 11px; }
.cap-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.cap-ports { display: flex; flex-wrap: wrap; gap: 6px; }
.cap-port { display: inline-flex; align-items: center; gap: 6px; background: #f7f9fb; border: 1px solid #edf1f4; border-radius: 6px; padding: 5px 10px; font-size: 12px; }
.port-func { color: #2c3e50; font-weight: 600; }
.mono { font-family: ui-monospace, monospace; }
</style>
