from rapidfuzz.distance import DamerauLevenshtein

FILLERS = {"мне", "пожалуйста", "пожалуйсто", "плиз", "плз", "про", "что", "чтобы"}


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _strip_fillers(text: str) -> str:
    words = text.split()
    while words and _norm(words[0]).strip(",.!?") in FILLERS:
        words.pop(0)
    return " ".join(words).strip()


def match_trigger(text: str, stems: list[str]) -> str | None:
    norm = _norm(text).strip()
    words = norm.split()
    for stem in stems:
        stem = _norm(stem)
        if " " in stem:
            idx = norm.find(stem)
            if idx == -1:
                continue
            rest = (norm[:idx] + " " + norm[idx + len(stem):]).strip()
            return _strip_fillers(rest)
        for i, w in enumerate(words):
            prefix = w.strip(",.!?")[: len(stem)]
            if DamerauLevenshtein.distance(prefix, stem) <= 1:
                rest = " ".join(words[:i] + words[i + 1:])
                return _strip_fillers(rest)
    return None
