(() => {
    const PLUGIN_ID = 'com.threadwise.mattermost';
    const STYLE_ELEMENT_ID = 'threadwise-header-branding-style';

    function getLogoUrl() {
        const basePath = window.basename || '';
        return basePath + '/plugins/' + PLUGIN_ID + '/public/threadwiselogo.png';
    }

    function createBrandingStyle() {
        const logoUrl = getLogoUrl();
        const style = document.createElement('style');
        style.id = STYLE_ELEMENT_ID;
        style.setAttribute('data-plugin-id', PLUGIN_ID);
        style.textContent = `
            .globalHeader-leftControls-productBranding-freeEdition > .globalHeader-leftControls-productBranding-freeEdition-logo {
                display: none !important;
            }
            .globalHeader-leftControls-productBranding-freeEdition::before {
                content: '';
                display: inline-block;
                height: 24px;
                width: 46px;
                max-width: 100%;
                background-image: url("${logoUrl}");
                background-repeat: no-repeat;
                background-position: left center;
                background-size: contain;
                border-radius: 4px;
                flex-shrink: 0;
            }
        `;
        return style;
    }

    function ensureStyleInjected() {
        if (!document.getElementById(STYLE_ELEMENT_ID)) {
            const style = createBrandingStyle();
            document.head.appendChild(style);
        }
    }

    function removeStyle() {
        const style = document.getElementById(STYLE_ELEMENT_ID);
        if (style) {
            style.remove();
        }
    }

    function ThreadWiseBrandingComponent() {
        const React = window.React;
        if (!React) {
            return null;
        }
        React.useEffect(() => {
            ensureStyleInjected();
            return () => {
                removeStyle();
            };
        }, []);
        return null;
    }

    class ThreadWisePlugin {
        initialize(registry, store) {
            ensureStyleInjected();
            if (registry && typeof registry.registerGlobalComponent === 'function') {
                this.componentId = registry.registerGlobalComponent(ThreadWiseBrandingComponent);
            }
        }

        uninitialize() {
            removeStyle();
        }
    }

    if (typeof window.registerPlugin === 'function') {
        window.registerPlugin(PLUGIN_ID, new ThreadWisePlugin());
    }
})();
