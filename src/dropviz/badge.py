"""A small static SVG badge for a README: verified features and the share of them built with AI tools. Counts only."""
from . import core, defaults, i18n


def numbers(state):
    done = [f for f in state["features"] if f["status"] == "done"]
    ai = [f for f in done if any(c["kind"] == "ai" for c in f["evidence"]["commits"])]
    return {"verified": len(done), "total": len(state["features"]), "ai_percent": (100.0 * len(ai) / len(done)) if done else 0.0}


def svg(state, lang=None, tokens_total=None):
    lang = lang or i18n.from_env()
    n = numbers(state)
    B = defaults.data()["badge"]
    left = i18n.t(lang, "badge.label")
    right = i18n.t(lang, "badge.value", n=n["verified"], ai=round(n["ai_percent"], defaults.get("badge_ai_percent_digits")).__format__(".0f"))
    if tokens_total is not None:
        right += " · " + i18n.t(lang, "badge.tokens", n="{:,}".format(tokens_total))
    w1 = int(len(left) * B["char_width"] + 2 * B["pad"])
    w2 = int(len(right) * B["char_width"] + 2 * B["pad"])
    h = B["height"]
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;")
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" role="img" aria-label="%s: %s">'
            '<rect width="%d" height="%d" rx="4" fill="%s"/><rect x="%d" width="%d" height="%d" rx="4" fill="%s"/>'
            '<rect x="%d" width="6" height="%d" fill="%s"/>'
            '<g fill="%s" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" text-anchor="middle">'
            '<text x="%d" y="%d">%s</text><text x="%d" y="%d">%s</text></g></svg>\n') % (
        w1 + w2, h, esc(left), esc(right), w1 + w2, h, B["label_fill"], w1, w2, h, B["value_fill"], w1, h, B["value_fill"],
        B["text_fill"], w1 // 2, h - 7, esc(left), w1 + w2 // 2, h - 7, esc(right))
