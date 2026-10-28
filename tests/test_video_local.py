import json
import shutil
import subprocess
import sys

import pytest

from feishu_chat_diagnose.config import CodexSettings, LocalVideoSettings
from feishu_chat_diagnose.video_local import LocalVideoError, LocalVideoUnderstanding


FAKE_CODEX = r'''
import json, sys
from pathlib import Path
config = {}
for index, arg in enumerate(sys.argv):
    if arg == '-c':
        key, value = sys.argv[index + 1].split('=', 1)
        target = config
        parts = key.split('.')
        for part in parts[:-1]: target = target.setdefault(part, {})
        target[parts[-1]] = json.loads(value)
config['mcp_servers'] = {'must_disable': {'enabled': True}}
def send(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req: continue
    method, params = req['method'], req['params']
    result = {}
    if method == 'config/read': result = {'config': config}
    if method == 'thread/start':
        assert params['config']['mcp_servers']['must_disable']['enabled'] is False
        assert params['sandbox'] == 'read-only'
        result = {'thread': {'id': 'video'}}
    if method == 'turn/start':
        images = [Path(i['path']) for i in params['input'] if i['type'] == 'localImage']
        assert images and all(p.read_bytes().startswith(bytes([255, 216])) for p in images)
        payload = json.loads(params['input'][0]['text'])
        assert payload['source_message_id'] == 'om_video'
        assert params['sandboxPolicy'] == {'type': 'readOnly'}
        Path(sys.argv[0]).with_suffix('.log').write_text(json.dumps([str(p) for p in images]))
    send({'id': req['id'], 'result': result})
    if method == 'turn/start':
        final = {'status': 'understood', 'result': {'source_message_id': 'om_video',
            'summary': '红色画面', 'observations': [{'timestamp': '00:00', 'observation': '红色背景'}],
            'missing_information': []}}
        send({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': json.dumps(final)}}})
'''


@pytest.mark.parametrize("understood", [True, False])
def test_real_decode_and_native_images_without_mcp_cleanup(tmp_path, understood):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are not installed")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=96x64:d=1",
        "-c:v", "libx264", str(source),
    ], check=True)
    original = source.read_bytes()
    codex = tmp_path / "fake-codex"
    script = FAKE_CODEX if understood else FAKE_CODEX.replace("'status': 'understood'", "'status': 'unreadable'")
    codex.write_text(f"#!{sys.executable}\n" + script)
    codex.chmod(0o700)
    decoder = LocalVideoUnderstanding(LocalVideoSettings(), CodexSettings(bin=str(codex)))
    if understood:
        result = decoder.understand_video(source, "om_video", "描述画面")
        assert result.summary == "红色画面"
        assert "抽样" in result.missing_information[-1]
    else:
        with pytest.raises(LocalVideoError, match="无法读取抽样画面"):
            decoder.understand_video(source, "om_video", "描述画面")
    assert source.read_bytes() == original
    from pathlib import Path
    assert all(not Path(p).exists() for p in json.loads(codex.with_suffix('.log').read_text()))


def test_decode_failure_cleans_partial_frames_and_never_starts_codex(tmp_path, mocker):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    decoder = LocalVideoUnderstanding(LocalVideoSettings(), CodexSettings())
    roots = []

    def fail(source, root, deadline):
        roots.append(root)
        (root / "partial.jpg").write_bytes(b"partial")
        raise LocalVideoError("解码失败")

    mocker.patch.object(decoder, "_extract", side_effect=fail)
    perceive = mocker.patch.object(decoder, "_perceive")
    with pytest.raises(LocalVideoError, match="解码失败"):
        decoder.understand_video(source, "om_video", "看画面")
    perceive.assert_not_called()
    assert roots and not roots[0].exists()


def test_long_video_sampling_covers_duration_and_respects_frame_budget(tmp_path, mocker):
    from pathlib import Path

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    decoder = LocalVideoUnderstanding(LocalVideoSettings(max_frames=3), CodexSettings())

    def decode(args, deadline):
        if args[0] == "ffprobe":
            return b'{"format":{"duration":"60"}}'
        Path(args[-1]).write_bytes(b"frame")
        return b""

    mocker.patch.object(decoder, "_run", side_effect=decode)
    frames = []

    def perceive(root, samples, *args):
        frames.extend(samples)
        return mocker.Mock()

    mocker.patch.object(decoder, "_perceive", side_effect=perceive)
    decoder.understand_video(source, "om_video", "看画面")
    assert [t for t, path in frames] == [0, 20, 40]
    assert all(not path.exists() for t, path in frames)
