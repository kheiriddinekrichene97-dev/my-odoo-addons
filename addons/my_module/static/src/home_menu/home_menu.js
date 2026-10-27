import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useBus, useService } from "@web/core/utils/hooks";

export const HOME_MENU_ACTION = "my_module.action_home_menu";

// Shared with the navbar patch so it can hide the previous app's menus
// while the home page is displayed.
export const homeMenuState = { isActive: false };

// Used when the company has no brand colours set (Settings > Companies).
const DEFAULT_COLORS = ["#5f3c5c", "#22394d"];

function normalize(text) {
    return (text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

export class HomeMenu extends Component {
    static template = "my_module.HomeMenu";
    static props = ["*"];

    setup() {
        this.menuService = useService("menu");
        this.orm = useService("orm");
        this.ui = useService("ui");
        this.searchRef = useRef("search");
        this.state = useState({ query: "", focusedIndex: 0 });
        this.company = { id: user.activeCompany?.id, name: user.activeCompany?.name || "", hasLogo: false };
        this.colors = DEFAULT_COLORS;
        useBus(this.env.bus, "MENUS:APP-CHANGED", () => this.render());
        onWillStart(() => this.loadBranding());
        onMounted(() => {
            this.setActive(true);
            // No autofocus on phones: it would pop the keyboard up.
            if (!this.ui.isSmall) {
                this.searchRef.el?.focus();
            }
        });
        onWillUnmount(() => this.setActive(false));
    }

    async loadBranding() {
        if (!this.company.id) {
            return;
        }
        try {
            const [company] = await this.orm.read(
                "res.company",
                [this.company.id],
                ["name", "primary_color", "secondary_color", "uses_default_logo"]
            );
            this.company.name = company.name;
            this.company.hasLogo = !company.uses_default_logo;
            if (company.primary_color) {
                this.colors = [company.primary_color, company.secondary_color || DEFAULT_COLORS[1]];
            }
        } catch {
            // Branding is cosmetic: keep the defaults.
        }
    }

    setActive(isActive) {
        homeMenuState.isActive = isActive;
        this.env.bus.trigger("MENUS:APP-CHANGED");
    }

    get backgroundStyle() {
        const [primary, secondary] = this.colors;
        return `background: radial-gradient(circle at 15% 0%, ${primary} 0%, transparent 60%), linear-gradient(135deg, ${primary} 0%, ${secondary} 100%);`;
    }

    get logoUrl() {
        return `/web/image/res.company/${this.company.id}/logo`;
    }

    get apps() {
        return this.menuService.getApps().map((app) => ({
            id: app.id,
            name: app.name,
            xmlid: app.xmlid,
            href: `/odoo/${app.actionPath || "action-" + app.actionID}`,
            menu: app,
            ...this.getIcon(app),
        }));
    }

    get visibleApps() {
        const query = normalize(this.state.query.trim());
        return query ? this.apps.filter((app) => normalize(app.name).includes(query)) : this.apps;
    }

    /**
     * Normalizes the app icon the same way the command palette does: either an
     * image (base64 png/svg or url), or a font icon on a colored background.
     */
    getIcon(app) {
        if (app.webIconData) {
            if (app.webIconData.startsWith("data:image") || app.webIconData.startsWith("/")) {
                return { webIconData: app.webIconData };
            }
            const prefix = app.webIconData.startsWith("P")
                ? "data:image/svg+xml;base64,"
                : "data:image/png;base64,";
            return { webIconData: prefix + app.webIconData.replace(/\s/g, "") };
        }
        const [iconClass, color, backgroundColor] = (app.webIcon || "").split(",");
        if (backgroundColor !== undefined) {
            return { webIcon: { iconClass, color, backgroundColor } };
        }
        return { webIconData: "/web/static/img/default_icon_app.png" };
    }

    onSearchInput(ev) {
        this.state.query = ev.target.value;
        this.state.focusedIndex = 0;
    }

    onSearchKeydown(ev) {
        const apps = this.visibleApps;
        if (ev.key === "Enter" && apps.length) {
            ev.preventDefault();
            this.openApp(apps[Math.min(this.state.focusedIndex, apps.length - 1)]);
        } else if (ev.key === "Escape") {
            this.state.query = "";
            this.state.focusedIndex = 0;
        } else if (["ArrowRight", "ArrowDown"].includes(ev.key) && apps.length) {
            ev.preventDefault();
            this.state.focusedIndex = (this.state.focusedIndex + 1) % apps.length;
        } else if (["ArrowLeft", "ArrowUp"].includes(ev.key) && apps.length) {
            ev.preventDefault();
            this.state.focusedIndex = (this.state.focusedIndex - 1 + apps.length) % apps.length;
        }
    }

    openApp(app) {
        this.menuService.selectMenu(app.menu);
    }

    onAppClick(ev, app) {
        ev.preventDefault();
        this.openApp(app);
    }
}

registry.category("actions").add("my_module.home_menu", HomeMenu);
