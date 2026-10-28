import { patch } from "@web/core/utils/patch";
import { NavBar } from "@web/webclient/navbar/navbar";
import { WebClient } from "@web/webclient/webclient";
import { HOME_MENU_ACTION, homeMenuState } from "../home_menu/home_menu";
import { SideMenu } from "../side_menu/side_menu";

NavBar.components = { ...NavBar.components, SideMenu };

patch(WebClient.prototype, {
    // Open the home page instead of the first app when no action is requested
    // (e.g. right after login). A user's own Home Action still takes precedence.
    _loadDefaultApp() {
        return this.actionService.doAction(HOME_MENU_ACTION, { clearBreadcrumbs: true });
    },
});

patch(NavBar.prototype, {
    get currentApp() {
        return homeMenuState.isActive ? undefined : super.currentApp;
    },
});
