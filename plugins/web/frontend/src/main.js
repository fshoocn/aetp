import { createApp } from 'vue';
import App from './App.vue';
import router from './router';
import { initializePluginUi } from './plugins/ui-runtime';

import Aura from '@primeuix/themes/aura';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';

import '@/assets/tailwind.css';
import '@/assets/styles.scss';

async function bootstrap() {
    const app = createApp(App);

    app.use(PrimeVue, {
        theme: {
            preset: Aura,
            options: {
                darkModeSelector: '.app-dark'
            }
        }
    });
    app.use(ToastService);
    app.use(ConfirmationService);

    try {
        await initializePluginUi();
    } catch (error) {
        console.error('插件 UI 清单加载失败', error);
    }

    app.use(router);
    await router.isReady();
    app.mount('#app');
}

bootstrap();
