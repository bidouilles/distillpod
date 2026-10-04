from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from services import youtube


def test_cookie_file_is_copied_privately_and_original_is_not_modified(tmp_path):
    source = tmp_path / 'cookies.txt'
    source.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-session\n')
    seen = []
    def run(command, **kwargs):
        cookie = Path(command[command.index('--cookies') + 1])
        assert cookie != source
        assert cookie.stat().st_mode & 0o777 == 0o600
        assert cookie.read_text() == source.read_text()
        cookie.write_text('updated by yt-dlp')
        seen.append(cookie)
        return 'ok'
    with patch.object(youtube.settings, 'ytdlp_cookies_file', str(source), create=True), \
         patch.object(youtube.subprocess, 'run', side_effect=run):
        assert youtube._run(['--version'], 10) == 'ok'
    assert 'fake-session' in source.read_text()
    assert not seen[0].exists()


def test_missing_configured_cookies_fail_explicitly(tmp_path):
    with patch.object(youtube.settings, 'ytdlp_cookies_file', str(tmp_path/'missing'), create=True):
        with pytest.raises(youtube.YouTubeError, match='cookie file'):
            youtube._run(['--version'], 10)


def test_cookie_values_are_removed_from_errors(tmp_path):
    source = tmp_path / 'cookies.txt'
    source.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-session-secret\n')
    failure = subprocess.CalledProcessError(1, 'yt-dlp', stderr='Invalid cookie: fake-session-secret')
    with patch.object(youtube.settings, 'ytdlp_cookies_file', str(source)), \
         patch.object(youtube.subprocess, 'run', side_effect=failure):
        with pytest.raises(youtube.YouTubeError) as error:
            youtube._run(['--version'], 10)
        assert 'fake-session-secret' not in str(error.value)
        assert '[redacted]' in str(error.value)
