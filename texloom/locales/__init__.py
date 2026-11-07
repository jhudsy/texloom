"""The desktop app's translations: one module per language, keyed by the English text.

To add a language: copy fr.py to <code>.py (an ISO 639-1 code such as de, es, pl),
translate the values, and add it below (and its name to NAMES). If its plurals aren't "one" for 1 and "other"
otherwise, add its rule to PLURAL_RULES in texloom/i18n.py. Check with
`python scripts/check_i18n.py`.
"""

from . import fr

CATALOGS = {
    "fr": fr.CATALOG,
}

# Each language's name in itself, for the language menu.
NAMES = {
    "en": "English",
    "fr": "Français",
}
