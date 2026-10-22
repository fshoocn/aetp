import { createApp } from 'vue';
import App from './App.vue';
import router from './router/index.js';
import { initializePluginUi } from './plugins/ui-runtime.js';
import { registerSharedComponents } from './plugins/shared-components.js';

import Aura from '@primeuix/themes/aura';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import Ripple from 'primevue/ripple';
import ToastService from 'primevue/toastservice';

import '@/assets/tailwind.css';
import '@/assets/styles.scss';

async function bootstrap() {
    const app = createApp(App);

    app.use(PrimeVue, {
        ripple: true,
        theme: {
            preset: Aura,
            options: {
                darkModeSelector: '.app-dark',
                // Tailwind 工具类可安全覆盖组件样式（PrimeVue 层在 theme/base 之后、utilities 之前）
                cssLayer: {
                    name: 'primevue',
                    order: 'theme, base, primevue'
                }
            }
        }
    });
    // v-ripple 全局可用（含插件 SFC）：宿主与主题面板的原生按钮加 v-ripple 即有点击动画
    app.directive('ripple', Ripple);
    app.use(ToastService);
    app.use(ConfirmationService);
    // 插件 SFC 的共享组件（运行时解析全局注册，见 src/plugins/shared-components.js）
    registerSharedComponents(app);

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
