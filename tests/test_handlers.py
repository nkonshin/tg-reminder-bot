from src.handlers import router


def test_router_named_and_wired():
    assert router.name == "reminder-bot"
    assert router.business_message.handlers
    assert router.callback_query.handlers
    assert router.message.handlers


def test_entrypoint_imports():
    import src.main

    assert callable(src.main.main)
