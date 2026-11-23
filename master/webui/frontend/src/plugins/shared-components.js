import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import IconField from 'primevue/iconfield';
import InputIcon from 'primevue/inputicon';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Menu from 'primevue/menu';
import Message from 'primevue/message';
import MultiSelect from 'primevue/multiselect';
import Panel from 'primevue/panel';
import RadioButton from 'primevue/radiobutton';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Timeline from 'primevue/timeline';
import PluginUiOutlet from '@/plugins/PluginUiOutlet.vue';

/**
 * 插件 UI 共享组件表。
 *
 * vue3-sfc-loader 运行时编译的插件 SFC 走不了 Vite 自动组件导入（见开发文档 §24），
 * 宿主在这里把常用 PrimeVue 组件注册为全局组件：插件页面可直接按名使用这些组件，
 * 与宿主页面共享同一套 Aura 主题样式。需要更多组件时按需扩充此清单。
 *
 * 命名约定（HTML 同名坑）：与 HTML 原生标签同名的组件（Dialog/Menu/Textarea）
 * 一律以 Pv 前缀别名注册（插件模板写 <PvDialog>/<PvMenu>/<PvTextarea>）；其余
 * 组件按原名注册、插件模板用 PascalCase 引用（如 <MultiSelect>）。
 */
const SHARED_COMPONENTS = {
    PluginUiOutlet,
    Button,
    Checkbox,
    Column,
    DataTable,
    // Dialog/Menu/Textarea 是 HTML 同名原生标签（dialog/menu/textarea），插件 SFC
    // 运行时编译会解析成原生元素（实测 <menu model="...">）——用 Pv 前缀别名注册
    // 规避，插件模板里写 <PvDialog>/<PvMenu>/<PvTextarea>，禁止小写同名写法
    PvDialog: Dialog,
    IconField,
    InputIcon,
    InputNumber,
    InputText,
    PvMenu: Menu,
    Message,
    MultiSelect,
    Panel,
    RadioButton,
    Select,
    Tag,
    PvTextarea: Textarea,
    Timeline
};

export function registerSharedComponents(app) {
    for (const [name, component] of Object.entries(SHARED_COMPONENTS)) {
        app.component(name, component);
    }
}
