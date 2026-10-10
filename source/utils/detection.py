import re


# Filename-only hints, never verified media tracks.
_SUBTITLE_HINT = re.compile(
    r'(?i)\b(KOR|KO|KOREAN|JPN|JA|JAPANESE|CHI|ZH|CHINESE)'
    r'[. _-]*(SUB|SUBS|SUBTITLE|SUBTITLES)\b'
)
_DUB_HINT = re.compile(
    r'(?i)\b(KOR|KO|KOREAN|JPN|JA|JAPANESE|CHI|ZH|CHINESE)'
    r'[. _-]*(DUB|DUBBED|AUDIO)\b'
)


def detect_subtitle_hint(torrent_name):
    match = _SUBTITLE_HINT.search(torrent_name or '')
    if not match:
        return None
    prefix = match.group(1).upper()
    return {'KOR': 'KO', 'KOREAN': 'KO',
            'JPN': 'JA', 'JAPANESE': 'JA',
            'CHI': 'ZH', 'CHINESE': 'ZH'}.get(prefix, prefix)


def detect_languages(torrent_name):
    language_patterns = {
        "fr": r'\b(FRENCH|FR|VF|VF2|VFF|TRUEFRENCH|VFQ|FRA)\b',
        "en": r'\b(ENGLISH|EN|ENG)\b',
        "es": r'\b(SPANISH|ES|ESP)\b',
        "de": r'\b(GERMAN|DE|GER)\b',
        "it": r'\b(ITALIAN|IT|ITA)\b',
        "pt": r'\b(PORTUGUESE|PT|POR)\b',
        "ru": r'\b(RUSSIAN|RU|RUS)\b',
        "in": r'\b(INDIAN|IN|HINDI|TELUGU|TAMIL|KANNADA|MALAYALAM|PUNJABI|MARATHI|BENGALI|GUJARATI|URDU|ODIA|ASSAMESE|KONKANI|MANIPURI|NEPALI|SANSKRIT|SINHALA|SINDHI|TIBETAN|BHOJPURI|DHIVEHI|KASHMIRI|KURUKH|MAITHILI|NEWARI|RAJASTHANI|SANTALI|SINDHI|TULU)\b',
        "nl": r'\b(DUTCH|NL|NLD)\b',
        "hu": r'\b(HUNGARIAN|HU|HUN)\b',
        "la": r'\b(LATIN|LATINO|LA)\b',
        "ko": r"\b(KOREAN|KOR|KO)\b",
        "ja": r"\b(JAPANESE|JPN|JA)\b",
        "zh": r"\b(CHINESE|CHI|ZH|MANDARIN|CANTONESE)\b",
        "multi": r"\b(MULTI)\b"
    }

    languages = []
    for language, pattern in language_patterns.items():
        if re.search(pattern, torrent_name, re.IGNORECASE):
            # A subtitle marker alone does not establish an audio language.
            if language in ('ko', 'ja', 'zh') and detect_subtitle_hint(torrent_name) == language.upper():
                if not _DUB_HINT.search(torrent_name):
                    continue
            languages.append(language)

    if len(languages) == 0:
        return ["unknown"]

    return languages
