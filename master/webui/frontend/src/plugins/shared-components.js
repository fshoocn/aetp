import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import IconField from 'primevue/iconfield';
import InputIcon from 'primevue/inputicon';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Panel from 'primevue/panel';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Timeline from 'primevue/timeline';

/**
 * 插件 UI 共享组件表。
 *
 * vue3-sfc-loader 运行时编译的插件 SFC 走不了 Vite 自动组件导入（见开发文档 §24），
 * 宿主在这里把常用 PrimeVue 组件注册为全局组件：插件页面可直接按名使用这些组件，
 * 与宿主页面共享同一套 Aura 主题样式。需要更多组件时按需扩充此清单。
 */
const SHARED_COMPONENTS = {
    Button,
    Column,
    DataTable,
    IconField,
    InputIcon,
    InputText,
    Message,
    Panel,
    Select,
    Tag,
    Timeline
};

export function registerSharedComponents(app) {
    for (const [name, component] of Object.entries(SHARED_COMPONENTS)) {
        app.component(name, component);
    }
}
