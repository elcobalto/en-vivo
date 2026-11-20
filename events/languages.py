"""The site's languages: Spanish by default, English per user (``Profile.language``).

New profiles start in the language of their country: Spanish for Spain and Latin America, English
elsewhere. Without a known country they get the site's default (Spanish).
"""
from __future__ import annotations

from django.conf import settings
from django.utils import translation

# Spain and Latin America (Brazil included: Spanish reads easier there than English).
SPANISH_FIRST_COUNTRIES = frozenset({
    "AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "ES", "GQ", "GT", "HN", "MX", "NI", "PA",
    "PE", "PR", "PY", "SV", "UY", "VE",
})


def site_language() -> str:
    """The language visitors see before they sign in ("es")."""
    return settings.LANGUAGE_CODE


def supported(code: str) -> str:
    """``code`` if the site is translated into it (e.g. "es" from "es-419"), else ""."""
    code = (code or "").strip().replace("_", "-").lower().split("-")[0]
    return code if code in dict(settings.LANGUAGES) else ""


def language_for_country(country_code: str) -> str:
    """Spanish for Spain and Latin America, English for other countries, the site default without one."""
    code = (country_code or "").strip().upper()
    if not code:
        return site_language()
    return "es" if code in SPANISH_FIRST_COUNTRIES else "en"


def language_from_locale(locale: str) -> str:
    """The language for a social account's locale ("es-419", "en_GB", "es"), or "" if it says nothing useful.

    Its country decides when it has one ("en-CL" is a Chilean: Spanish); otherwise its own language, if we
    have it.
    """
    parts = (locale or "").strip().replace("_", "-").split("-")
    region = parts[1] if len(parts) > 1 else ""
    if len(region) == 2 and region.isalpha():
        return language_for_country(region)
    return supported(parts[0])


def user_language(user) -> str:
    """The language a request is shown in: the signed-in user's choice, else the site default."""
    if user is not None and getattr(user, "is_authenticated", False):
        profile = getattr(user, "profile", None)  # a missing profile raises an AttributeError subclass
        language = supported(getattr(profile, "language", ""))
        if language:
            return language
    return site_language()


class ProfileLanguageMiddleware:
    """Shows each page in the signed-in user's language (from their profile); visitors get Spanish.

    Goes after AuthenticationMiddleware. Unlike Django's LocaleMiddleware it ignores the browser's
    Accept-Language: the site is Spanish unless you choose English on your profile.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = user_language(getattr(request, "user", None))
        translation.activate(language)
        request.LANGUAGE_CODE = language
        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()
        response.headers.setdefault("Content-Language", language)
        return response
