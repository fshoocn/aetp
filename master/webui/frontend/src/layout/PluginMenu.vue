<script setup>
import AppMenuItem from './AppMenuItem.vue';
import { menuModel } from './composables/menu.js';
</script>

<template>
    <ul class="layout-menu">
        <template v-for="(item, index) in menuModel" :key="item.label">
            <app-menu-item :item="item" :index="index" />
        </template>
    </ul>
</template>

<style lang="scss" scoped></style>
