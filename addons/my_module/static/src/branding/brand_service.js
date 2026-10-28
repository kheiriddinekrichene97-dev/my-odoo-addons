import { reactive } from "@odoo/owl";
import { titleService } from "@web/core/browser/title_service";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { patch } from "@web/core/utils/patch";

/**
 * The active company's brand - name, tagline, logo, colours - read from the
 * company record (Settings > Companies), so changing it there changes the
 * whole interface: top bar, browser tab title and icon, home page.
 * A company without brand colours keeps Odoo's own look.
 */

let brandTitle = "";

// The tab title ends with the brand ("Receipts - Dimensione Immagine"),
// instead of falling back to "Odoo".
patch(titleService, {
    start() {
        const service = super.start(...arguments);
        const setParts = service.setParts;
        service.setParts = (parts) => {
            setParts({ ...parts, zz_brand: null });
            if (brandTitle) {
                setParts({ zz_brand: brandTitle });
            }
        };
        return service;
    },
});

function plainText(html) {
    return new DOMParser().parseFromString(html || "", "text/html").body.textContent.trim();
}

function isLight(hex) {
    const match = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || "");
    if (!match) {
        return false;
    }
    const [r, g, b] = match.slice(1).map((c) => parseInt(c, 16) / 255);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6;
}

export const brandService = {
    dependencies: ["orm", "title"],

    start(env, { orm, title }) {
        const company = user.activeCompany;
        const brand = reactive({
            loaded: false,
            companyId: company?.id,
            name: company?.name || "",
            tagline: "",
            hasLogo: false,
            logoUrl: "",
            primary: false,
            accent: false,
        });

        function apply() {
            const root = document.documentElement;
            root.classList.toggle("o_my_branded", Boolean(brand.primary));
            root.classList.toggle("o_my_brand_light", isLight(brand.primary));
            if (brand.primary) {
                root.style.setProperty("--my-brand-primary", brand.primary);
                root.style.setProperty("--my-brand-accent", brand.accent || brand.primary);
            }
            if (brand.hasLogo) {
                for (const link of document.querySelectorAll("link[rel~='icon']")) {
                    link.href = brand.logoUrl;
                }
            }
            brandTitle = brand.name;
            title.setParts({});
        }

        async function load() {
            if (!brand.companyId) {
                return;
            }
            try {
                const [record] = await orm.read(
                    "res.company",
                    [brand.companyId],
                    ["name", "report_header", "primary_color", "secondary_color", "uses_default_logo", "write_date"]
                );
                Object.assign(brand, {
                    name: record.name,
                    tagline: plainText(record.report_header),
                    hasLogo: !record.uses_default_logo,
                    logoUrl: `/web/image/res.company/${brand.companyId}/logo?unique=${encodeURIComponent(record.write_date)}`,
                    primary: record.primary_color || false,
                    accent: record.secondary_color || false,
                });
            } catch {
                // The brand is cosmetic: never block the interface for it.
            }
            brand.loaded = true;
            apply();
        }

        apply();
        load();
        return brand;
    },
};

registry.category("services").add("my_brand", brandService);
