import os
import stat
from pathlib import Path

from sieve import cli


def test_social_login_bootstraps_dedicated_profile(monkeypatch, tmp_path: Path, capsys):
    class FakePage:
        async def goto(self, url, *, wait_until):
            assert url == "https://www.instagram.com/accounts/login/"
            assert wait_until == "domcontentloaded"

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeBrowser:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._context = FakeContext()
            self.closed = False
            self.instances.append(self)

        async def start(self):
            return None

        async def close(self):
            self.closed = True

    monkeypatch.setattr("sieve.browser.StealthyBrowser", FakeBrowser)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    profile = tmp_path / "instagram"
    previous_umask = os.umask(0) if os.name == "posix" else None
    try:
        result = cli._run_social_login(
            ["social", "login", "instagram", "--profile-dir", str(profile)]
        )
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)

    assert result == 0
    assert profile.is_dir()
    assert FakeBrowser.instances[0].kwargs == {
        "headless": False,
        "real_chrome": False,
        "cdp_url": None,
        "profile_dir": str(profile),
        "block_ads": False,
    }
    assert FakeBrowser.instances[0].closed
    if os.name != "nt":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert '"profile_ready": true' in capsys.readouterr().out
