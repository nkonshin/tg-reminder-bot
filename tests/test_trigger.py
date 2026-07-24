from src.trigger import match_trigger

STEMS = ["напомн", "не забудь", "поставь напоминание"]


def test_basic_trigger_strips_word_and_fillers():
    assert match_trigger("Напомни мне завтра посмотреть анализы", STEMS) == "завтра посмотреть анализы"


def test_fuzzy_typo_in_trigger():
    assert match_trigger("напмони про анализы", STEMS) == "анализы"


def test_multiword_stem():
    assert match_trigger("не забудь купить молоко", STEMS) == "купить молоко"


def test_yo_normalization():
    assert match_trigger("напомни, пожалуйста, про приём", STEMS) == "прием"


def test_no_trigger():
    assert match_trigger("просто болтаем", STEMS) is None


def test_multiword_stem_wins_over_single_word_stem():
    assert match_trigger("поставь напоминание купить хлеб", STEMS) == "купить хлеб"


def test_multiword_stem_inflected_form():
    assert match_trigger("не забудьте купить молоко", STEMS) == "купить молоко"


def test_multiword_stem_not_matched_inside_other_word():
    assert match_trigger("оне забудь купить молоко", STEMS) != "купить молоко"


def test_filler_stripped_immediately_after_trigger():
    assert (
        match_trigger("слушай, напомни мне пожалуйста купить хлеб", STEMS)
        == "слушай, купить хлеб"
    )
