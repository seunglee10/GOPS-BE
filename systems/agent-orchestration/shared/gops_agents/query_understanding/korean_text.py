from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable

CHOSEONG = ("ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ")
JUNGSEONG = ("ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ", "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ")
JONGSEONG = ("", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ")
CHOSEONG_SET = set(CHOSEONG)

HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
HANGUL_CYCLE = 21 * 28


def normalize_query_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip().lower()


def compact_text(value: object) -> str:
    text = normalize_query_text(value)
    return "".join(char for char in text if char.isalnum() or is_hangul_jamo(char))


def is_hangul_syllable(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return HANGUL_BASE <= code <= HANGUL_END


def is_hangul_jamo(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return 0x3130 <= code <= 0x318F or 0x1100 <= code <= 0x11FF


def choseong_key(value: object) -> str:
    result = []
    for char in normalize_query_text(value):
        if is_hangul_syllable(char):
            result.append(CHOSEONG[(ord(char) - HANGUL_BASE) // HANGUL_CYCLE])
        elif char in CHOSEONG_SET:
            result.append(char)
        elif char.isascii() and char.isalnum():
            result.append(char)
    return "".join(result)


def jamo_key(value: object) -> str:
    result = []
    for char in normalize_query_text(value):
        if is_hangul_syllable(char):
            offset = ord(char) - HANGUL_BASE
            cho = offset // HANGUL_CYCLE
            jung = (offset % HANGUL_CYCLE) // 28
            jong = offset % 28
            result.append(CHOSEONG[cho])
            result.append(JUNGSEONG[jung])
            if JONGSEONG[jong]:
                result.extend(JONGSEONG[jong])
        elif char.isalnum() or is_hangul_jamo(char):
            result.append(char)
    return "".join(result)


def choseong_tokens(value: object, min_length: int = 2) -> list[str]:
    return [match.group(0) for match in re.finditer(r"[ㄱ-ㅎ]+", normalize_query_text(value)) if len(match.group(0)) >= min_length]


def query_fragments(value: object, *, min_length: int = 2, max_length: int = 18) -> list[str]:
    text = normalize_query_text(value)
    base_tokens = re.findall(r"[a-z0-9가-힣ㄱ-ㅎㅏ-ㅣ.]+", text)
    compact = compact_text(text)
    fragments: list[str] = []
    for token in [*base_tokens, compact]:
        for fragment in compact_windows(token, min_length=min_length, max_length=max_length):
            if fragment not in fragments:
                fragments.append(fragment)
    return fragments


def compact_windows(value: object, *, min_length: int = 2, max_length: int = 18) -> Iterable[str]:
    text = compact_text(value)
    if len(text) < min_length:
        return []
    limit = min(len(text), max_length)
    windows: list[str] = []
    for size in range(min_length, limit + 1):
        for start in range(0, len(text) - size + 1):
            windows.append(text[start : start + size])
    if text not in windows:
        windows.append(text)
    return windows


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()
