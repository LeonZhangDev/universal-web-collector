import asyncio
from types import SimpleNamespace


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


def test_server_bounds_graceful_shutdown(monkeypatch):
    import main

    calls = []
    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs))),
    )

    main.run_server()

    assert calls == [
        (
            (main.app,),
            {
                "host": main.settings.host,
                "port": main.settings.port,
                "timeout_graceful_shutdown": 5,
            },
        )
    ]
