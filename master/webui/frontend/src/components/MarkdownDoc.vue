<script setup>
import { computed } from 'vue';

import { renderMarkdown } from '@/utils/markdown';

const props = defineProps({
    source: {
        type: String,
        default: ''
    }
});

// renderMarkdown 转义优先，输出不含原始 HTML，可安全交给 v-html
const content = computed(() => renderMarkdown(props.source));
</script>

<template>
    <div class="markdown-doc" v-html="content"></div>
</template>

<style scoped>
.markdown-doc {
    color: var(--p-text-color);
    font-size: 0.925rem;
    line-height: 1.7;
    overflow-wrap: anywhere;
}
.markdown-doc :deep(h1),
.markdown-doc :deep(h2),
.markdown-doc :deep(h3),
.markdown-doc :deep(h4),
.markdown-doc :deep(h5),
.markdown-doc :deep(h6) {
    margin: 1.2em 0 0.5em;
    line-height: 1.3;
}
.markdown-doc :deep(h1) {
    font-size: 1.45rem;
}
.markdown-doc :deep(h2) {
    font-size: 1.25rem;
}
.markdown-doc :deep(h3) {
    font-size: 1.1rem;
}
.markdown-doc :deep(h4),
.markdown-doc :deep(h5),
.markdown-doc :deep(h6) {
    font-size: 1rem;
}
.markdown-doc :deep(h1:first-child),
.markdown-doc :deep(h2:first-child),
.markdown-doc :deep(h3:first-child) {
    margin-top: 0;
}
.markdown-doc :deep(p) {
    margin: 0.6em 0;
}
.markdown-doc :deep(ul),
.markdown-doc :deep(ol) {
    margin: 0.6em 0;
    padding-left: 1.4em;
}
.markdown-doc :deep(li) {
    margin: 0.25em 0;
}
.markdown-doc :deep(code) {
    padding: 0.12em 0.35em;
    border-radius: 0.25rem;
    background: var(--p-content-hover-background);
    font-family: ui-monospace, Consolas, 'Courier New', monospace;
    font-size: 0.86em;
}
.markdown-doc :deep(pre) {
    margin: 0.8em 0;
    padding: 0.85rem 1rem;
    border: 1px solid var(--p-content-border-color);
    border-radius: 0.4rem;
    background: var(--p-content-hover-background);
    overflow-x: auto;
}
.markdown-doc :deep(pre code) {
    padding: 0;
    background: transparent;
    font-size: 0.82rem;
    line-height: 1.55;
}
.markdown-doc :deep(a) {
    color: var(--p-primary-color);
    text-decoration: underline;
}
.markdown-doc :deep(blockquote) {
    margin: 0.8em 0;
    padding: 0.2em 0 0.2em 0.9em;
    border-left: 3px solid var(--p-content-border-color);
    color: var(--p-text-muted-color);
}
.markdown-doc :deep(hr) {
    margin: 1.2em 0;
    border: 0;
    border-top: 1px solid var(--p-content-border-color);
}
.markdown-doc :deep(strong) {
    font-weight: 700;
}
</style>
