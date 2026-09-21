import asyncio


def test_lifespan_waits_for_executor_threads(monkeypatch):
    import main

    shutdown_wait_values = []
    monkeypatch.setattr(main, "recover_orphans", lambda: None)
    monkeypatch.setattr(
        main.task_manager,
        "shutdown",
        lambda wait=False: shutdown_wait_values.append(wait),
    )

    async def exercise_lifespan():
        async with main.lifespan(None):
            pass

    asyncio.run(exercise_lifespan())

    assert shutdown_wait_values == [True]
