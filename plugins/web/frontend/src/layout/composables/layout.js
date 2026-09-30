import { computed, reactive, watch } from 'vue';

const LAYOUT_STORAGE_KEY = 'aetp.layout.preferences';

const defaultLayoutConfig = {
    preset: 'Aura',
    primary: 'emerald',
    surface: null,
    darkTheme: false,
    menuMode: 'static'
};

function readLayoutConfig() {
    if (typeof window === 'undefined') {
        return { ...defaultLayoutConfig };
    }

    try {
        const stored = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) ?? 'null');
        if (!stored || typeof stored !== 'object') {
            return { ...defaultLayoutConfig };
        }

        return {
            preset: typeof stored.preset === 'string' ? stored.preset : defaultLayoutConfig.preset,
            primary: typeof stored.primary === 'string' ? stored.primary : defaultLayoutConfig.primary,
            surface: typeof stored.surface === 'string' ? stored.surface : null,
            darkTheme: typeof stored.darkTheme === 'boolean' ? stored.darkTheme : defaultLayoutConfig.darkTheme,
            menuMode: stored.menuMode === 'overlay' ? 'overlay' : 'static'
        };
    } catch {
        return { ...defaultLayoutConfig };
    }
}

const layoutConfig = reactive(readLayoutConfig());

if (typeof document !== 'undefined') {
    document.documentElement.classList.toggle('app-dark', layoutConfig.darkTheme);
}

watch(
    layoutConfig,
    (config) => {
        try {
            window.localStorage.setItem(
                LAYOUT_STORAGE_KEY,
                JSON.stringify({
                    preset: config.preset,
                    primary: config.primary,
                    surface: config.surface,
                    darkTheme: config.darkTheme,
                    menuMode: config.menuMode
                })
            );
        } catch {
            // Browser storage may be unavailable or full.
        }
    },
    { deep: true }
);

const layoutState = reactive({
    staticMenuInactive: false,
    overlayMenuActive: false,
    profileSidebarVisible: false,
    configSidebarVisible: false,
    sidebarExpanded: false,
    menuHoverActive: false,
    activeMenuItem: null,
    activePath: null
});

export function useLayout() {
    const toggleDarkMode = () => {
        if (!document.startViewTransition) {
            executeDarkModeToggle();

            return;
        }

        document.startViewTransition(() => executeDarkModeToggle(event));
    };

    const executeDarkModeToggle = () => {
        layoutConfig.darkTheme = !layoutConfig.darkTheme;
        document.documentElement.classList.toggle('app-dark');
    };

    const toggleMenu = () => {
        if (isDesktop()) {
            if (layoutConfig.menuMode === 'static') {
                layoutState.staticMenuInactive = !layoutState.staticMenuInactive;
            }

            if (layoutConfig.menuMode === 'overlay') {
                layoutState.overlayMenuActive = !layoutState.overlayMenuActive;
            }
        } else {
            layoutState.mobileMenuActive = !layoutState.mobileMenuActive;
        }
    };

    const toggleConfigSidebar = () => {
        layoutState.configSidebarVisible = !layoutState.configSidebarVisible;
    };

    const hideMobileMenu = () => {
        layoutState.mobileMenuActive = false;
    };

    const changeMenuMode = (event) => {
        layoutConfig.menuMode = event.value;
        layoutState.staticMenuInactive = false;
        layoutState.mobileMenuActive = false;
        layoutState.sidebarExpanded = false;
        layoutState.menuHoverActive = false;
        layoutState.anchored = false;
    };

    const isDarkTheme = computed(() => layoutConfig.darkTheme);
    const isDesktop = () => window.innerWidth > 991;

    const hasOpenOverlay = computed(() => layoutState.overlayMenuActive);

    return {
        layoutConfig,
        layoutState,
        isDarkTheme,
        toggleDarkMode,
        toggleConfigSidebar,
        toggleMenu,
        hideMobileMenu,
        changeMenuMode,
        isDesktop,
        hasOpenOverlay
    };
}
