"""
i18n.py - Çok Dilli Arayüz Desteği
====================================
JSON tabanlı yerelleştirme sistemi.
"""

import json
from pathlib import Path
from typing import Dict, Optional

I18N_DIR = Path(__file__).resolve().parent / "i18n"
_cache: Dict[str, Dict] = {}

SUPPORTED_LANGUAGES = {
    "tr": "Türkçe",
    "en": "English",
}

DEFAULT_LANG = "tr"


def load_translations(lang: str) -> Dict:
    """Dil dosyasını yükle (cache'li)."""
    if lang in _cache:
        return _cache[lang]
    path = I18N_DIR / f"{lang}.json"
    if not path.exists():
        path = I18N_DIR / f"{DEFAULT_LANG}.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _cache[lang] = data
    return data


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """
    Çeviri yap. Nested keys: 'nav.dashboard'
    Placeholder: {count} -> kwargs['count']
    """
    data = load_translations(lang)
    parts = key.split(".")
    val = data
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            val = None
            break
    if val is None:
        return key
    if isinstance(val, str) and kwargs:
        try:
            val = val.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return str(val)


def get_all_translations(lang: str) -> Dict:
    """Tüm çevirileri flat dict olarak döndür."""
    return load_translations(lang)
