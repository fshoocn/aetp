<script setup>
import AppConfigurator from '@/layout/AppConfigurator.vue';
import { useLayout } from '@/layout/composables/layout';
import { ref } from 'vue';

const { toggleDarkMode, isDarkTheme } = useLayout();

const configPopover = ref(null);

function toggleConfigPanel(event) {
    configPopover.value?.toggle(event);
}
</script>

<template>
    <div class="fixed flex gap-4 top-8 right-8">
        <Button type="button" @click="toggleDarkMode" rounded :icon="isDarkTheme ? 'pi pi-moon' : 'pi pi-sun'" severity="secondary" />
        <Button icon="pi pi-palette" type="button" rounded severity="secondary" @click="toggleConfigPanel" />
        <Popover ref="configPopover" :show-close-icon="false">
            <AppConfigurator />
        </Popover>
    </div>
</template>
