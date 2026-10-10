<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { PluginLoadedSfc } from './ui-runtime';
import { pluginUiState } from './ui-state';
import { resolveUrl } from '@/service/backend';

const props = defineProps({
    name: { type: String, required: true }
});

const contributions = computed(() =>
    [...pluginUiState.contributions, ...pluginUiState.runtimeSlots].filter((item) => item.kind === 'slot' && item.target === props.name).sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))
);

const scriptValues = computed(() => Object.values(pluginUiState.scriptValues).filter((item) => item.target === props.name));

const html = ref({});
const htmlErrors = ref({});
const requests = new Map();

async function loadHtml(contribution) {
    if (contribution.content !== undefined) {
        html.value[contribution.id] = contribution.content;
        return;
    }

    const requestId = (requests.get(contribution.id) || 0) + 1;
    requests.set(contribution.id, requestId);
    try {
        const response = await fetch(resolveUrl(contribution.entry));
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const content = await response.text();
        if (requests.get(contribution.id) === requestId) html.value[contribution.id] = content;
    } catch (error) {
        htmlErrors.value[contribution.id] = error;
        console.error(`插件 HTML 片段加载失败: ${contribution.id}`, error);
    }
}

function onContributionChange(items) {
    for (const item of items) {
        if (item.format === 'html' && !(item.id in html.value)) loadHtml(item);
    }
}

onBeforeUnmount(() => {
    for (const [id, requestId] of requests) requests.set(id, requestId + 1);
});

watch(contributions, onContributionChange, { immediate: true });
</script>

<template>
    <div class="plugin-ui-outlet" :data-outlet="name">
        <template v-for="item in contributions" :key="item.id">
            <div v-if="item.format === 'html' && html[item.id] !== undefined" class="plugin-ui-html" :data-plugin="item.owner" v-html="html[item.id]" />
            <PluginLoadedSfc v-else-if="item.format === 'vue'" :contribution="item" />
            <div v-else-if="htmlErrors[item.id]" class="plugin-ui-error">插件内容加载失败</div>
        </template>
        <div v-for="item in scriptValues" :key="item.contributionId + item.target" class="plugin-ui-script-value" :data-plugin="item.contributionId">
            {{ item.value }}
        </div>
    </div>
</template>
