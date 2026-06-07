import importlib

from mthydra.ru_agent import debug_poll


def test_tick_enables_on_flag_appear_and_disables_on_remove(tmp_path):
    flag = tmp_path / "debug.flag"
    calls = []
    poller = debug_poll.DebugPoller(
        flag_path=flag,
        enable_fn=lambda: calls.append("on"),
        disable_fn=lambda: calls.append("off"),
    )
    poller.tick()                 # no flag -> nothing
    assert calls == []
    flag.write_text("")           # flag appears
    poller.tick()
    assert calls == ["on"]
    poller.tick()                 # still present -> no repeat
    assert calls == ["on"]
    flag.unlink()                 # flag removed
    poller.tick()
    assert calls == ["on", "off"]


def test_tick_swallows_enable_errors(tmp_path):
    flag = tmp_path / "debug.flag"
    flag.write_text("")

    def boom():
        raise OSError("tmpfs full")

    poller = debug_poll.DebugPoller(flag_path=flag, enable_fn=boom,
                                    disable_fn=lambda: None)
    poller.tick()  # must not raise; stays disarmed so it can retry next tick
    assert poller.enabled is False


def test_agent_main_module_imports():
    # __main__ wires debug_poll; importing it must not raise.
    importlib.import_module("mthydra.ru_agent.__main__")
