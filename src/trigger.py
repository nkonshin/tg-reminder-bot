import re

from rapidfuzz.distance import DamerauLevenshtein

FILLERS = {"мне", "пожалуйста", "пожалуйсто", "плиз", "плз", "про", "что", "чтобы"}


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _strip_fillers(text: str) -> str:
    words = text.split()
    while words and _norm(words[0]).strip(",.!?") in FILLERS:
        words.pop(0)
    return " ".join(words).strip()


def _combine(before: str, after: str) -> str:
    after = _strip_fillers(after)
    return " ".join(part for part in (before.strip(), after) if part).strip()


def match_trigger(text: str, stems: list[str]) -> str | None:
    norm = _norm(text).strip()
    words = norm.split()
    ordered_stems = sorted(stems, key=lambda s: (len(s.split()), len(s)), reverse=True)
    for stem in ordered_stems:
        stem = _norm(stem)
        if " " in stem:
            pattern = re.compile(r"(?<!\w)" + re.escape(stem) + r"\w*")
            m = pattern.search(norm)
            if not m:
                continue
            return _combine(norm[: m.start()], norm[m.end():])
        for i, w in enumerate(words):
            prefix = w.strip(",.!?")[: len(stem)]
            if DamerauLevenshtein.distance(prefix, stem) <= 1:
                before = " ".join(words[:i])
                after = " ".join(words[i + 1:])
                return _combine(before, after)
    return None
