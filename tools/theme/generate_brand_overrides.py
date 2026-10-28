"""Generate CSS that repaints Odoo's primary purple with the company brand.

Odoo's stylesheets are compiled with its primary colour (#71639e) and shades of
it baked in. This scans a compiled stylesheet, keeps every declaration that uses
that colour family and rewrites it with the brand CSS variables set by
my_module's brand service, scoped to `html.o_my_branded` (companies with brand
colours):

- text colours                 -> --my-brand-text (readable shade of the accent)
- backgrounds, borders, shadows -> --my-brand-ui   (the brand primary colour)
- the same for Bootstrap/Odoo custom properties (--btn-bg, --link-color, ...),
  "r, g, b" triplets (--primary-rgb) becoming --my-brand-ui-rgb / -text-rgb

Darker/lighter shades keep their tone (color-mix with black/white), rgba()
transparency is kept. Re-run after upgrading Odoo:

    docker exec -i my-odoo-addons-odoo-1 odoo shell -d dev ... <<< \
        'open("/tmp/backend.css","w").write(env["ir.qweb"]._get_asset_bundle("web.assets_backend", debug_assets=True).css().raw.decode())'
    docker cp my-odoo-addons-odoo-1:/tmp/backend.css .
    python tools/theme/generate_brand_overrides.py backend.css addons/my_module/static/src/branding/brand_overrides.css
"""
import colorsys
import re
import sys

BASE = (0x71, 0x63, 0x9E)
SCOPE = "html.o_my_branded"
SKIP_AT_RULES = ("@keyframes", "@-webkit-keyframes", "@font-face", "@property")
COLOR_RE = re.compile(r"#([0-9a-fA-F]{6})\b|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")


def hls(rgb):
    return colorsys.rgb_to_hls(*(c / 255 for c in rgb))


BASE_H, BASE_L, BASE_S = hls(BASE)


def in_family(rgb):
    """Shades of the primary purple: same hue, similar saturation."""
    h, l, s = hls(rgb)
    if s < 0.12 or l < 0.08 or l > 0.985:
        return False
    return abs(h - BASE_H) < 0.035 and abs(s - BASE_S) < 0.22


def replacement(rgb, alpha, var):
    _h, l, _s = hls(rgb)
    if abs(l - BASE_L) < 0.02:
        color = f"var({var})"
    elif l < BASE_L:
        color = f"color-mix(in srgb, var({var}) {round(l / BASE_L * 100)}%, #000)"
    else:
        color = f"color-mix(in srgb, var({var}) {round((1 - l) / (1 - BASE_L) * 100)}%, #fff)"
    if alpha is not None and float(alpha) < 1:
        color = f"color-mix(in srgb, {color} {round(float(alpha) * 100)}%, transparent)"
    return color


TRIPLET_RE = re.compile(r"^\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*$")
TEXT_PROPS = ("color", "-webkit-text-fill-color", "caret-color")
NON_TEXT_WORDS = ("bg", "background", "border", "shadow", "outline", "fill")


def is_text(prop):
    """Text colour, as opposed to backgrounds/borders/shadows (also for
    custom properties such as --link-color vs --btn-bg)."""
    if prop.startswith("--"):
        name = prop.removesuffix("-rgb")
        return name.endswith("color") and not any(word in name for word in NON_TEXT_WORDS)
    return prop in TEXT_PROPS


def rewrite_value(prop, value):
    var = "--my-brand-text" if is_text(prop) else "--my-brand-ui"
    triplet = TRIPLET_RE.match(value)
    if prop.startswith("--") and triplet:
        # e.g. --primary-rgb: 113, 99, 158 (used as rgba(var(--primary-rgb), .5))
        # A triplet cannot hold a color-mix(): every shade maps to the brand colour itself
        rgb = tuple(round(float(c)) for c in triplet.groups())
        return f"var({var}-rgb)" if in_family(rgb) else None
    changed = False

    def sub(match):
        nonlocal changed
        if match.group(1):
            rgb, alpha = tuple(int(match.group(1)[i:i + 2], 16) for i in (0, 2, 4)), None
        else:
            rgb, alpha = tuple(int(match.group(i)) for i in (2, 3, 4)), match.group(5)
        if not in_family(rgb):
            return match.group(0)
        changed = True
        return replacement(rgb, alpha, var)

    new = COLOR_RE.sub(sub, value)
    return new if changed else None


def split_blocks(css):
    """Yield (prelude, body) for top-level blocks of a CSS text."""
    i, n = 0, len(css)
    while i < n:
        start = css.find("{", i)
        if start == -1:
            return
        prelude = css[i:start].strip()
        depth, j = 1, start + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        yield prelude, css[start + 1:j - 1]
        i = j


def scope_selector(selectors):
    scoped = []
    for selector in selectors.split(","):
        selector = selector.strip()
        if not selector:
            continue
        if selector.startswith(("html", ":root")):
            selector = re.sub(r"^(html|:root)", SCOPE, selector)
        else:
            selector = f"{SCOPE} {selector}"
        scoped.append(selector)
    return ",\n".join(scoped)


def process(css):
    out = []
    for prelude, body in split_blocks(css):
        if prelude.startswith("@"):
            if prelude.startswith(SKIP_AT_RULES):
                continue
            inner = process(body)
            if inner:
                out.append(f"{prelude} {{\n{inner}\n}}")
            continue
        declarations = []
        for decl in body.split(";"):
            if ":" not in decl or "{" in decl:
                continue
            prop, value = decl.split(":", 1)
            prop = prop.strip().lower()
            new = rewrite_value(prop, value.strip())
            if new:
                declarations.append(f"    {prop}: {new};")
        if declarations:
            out.append(f"{scope_selector(prelude)} {{\n" + "\n".join(declarations) + "\n}")
    return "\n".join(out)


def main(src, dst):
    css = re.sub(r"/\*.*?\*/", "", open(src, encoding="utf-8").read(), flags=re.S)
    result = process(css)
    header = ("/* Generated by tools/theme/generate_brand_overrides.py - do not edit.\n"
              " * Odoo's primary colour family repainted with the company brand. */\n")
    open(dst, "w", encoding="utf-8", newline="\n").write(header + result + "\n")
    print(f"{result.count('{') - result.count('@media')} rules -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
