"""agent-cli 노브 표 (`agent-cli knobs --json`) → 보드의 검증·spawn 인자 (v1.36.0).

보드는 노브 이름·플래그를 손으로 적지 않는다. 여기서 고정하는 것: 표의 종류별
검증(빈 값=기본, 선택지, bool 글자, 숫자, 기간, 미지 이름 거부), 표 순서의 인자
변환(bool 은 --x/--no-x), 표 없는 구버전은 모델만, 그리고 실 바이너리와의 계약.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent_board import knobs_spec

# autouse 픽스처가 `knobs_spec.load` 를 가짜로 바꾸기 전(수집 시점)에 원함수를 잡아 둔다
_REAL_LOAD = knobs_spec.load.__wrapped__  # lru_cache 를 벗긴 원함수
from conftest import FAKE_SPEC  # tests/ 가 sys.path 에 오른다 (prepend import mode)


class TestValidate:
    def test_empty_values_are_dropped(self):
        assert knobs_spec.validate(
            FAKE_SPEC, {"model": "m", "thinking": "", "stall": None}
        ) == {"model": "m"}

    def test_choice_is_lowercased_and_checked(self):
        assert knobs_spec.validate(FAKE_SPEC, {"thinking": "ON"}) == {"thinking": "on"}
        with pytest.raises(ValueError, match="thinking"):
            knobs_spec.validate(FAKE_SPEC, {"thinking": "sometimes"})

    @pytest.mark.parametrize(
        "given, expected",
        [
            (True, True),
            ("on", True),
            ("1", True),
            ("off", False),
            ("false", False),
            ("0", False),
        ],
    )
    def test_bool_words(self, given, expected):
        assert knobs_spec.validate(FAKE_SPEC, {"auditor": given}) == {
            "auditor": expected
        }

    def test_bool_rejects_other_words(self):
        with pytest.raises(ValueError):
            knobs_spec.validate(FAKE_SPEC, {"auditor": "maybe"})

    def test_number_is_numeric(self):
        assert knobs_spec.validate(FAKE_SPEC, {"max_agents": "3"}) == {"max_agents": 3}
        assert knobs_spec.validate(FAKE_SPEC, {"max_agents": 2.5}) == {
            "max_agents": 2.5
        }
        with pytest.raises(ValueError, match="max_agents"):
            knobs_spec.validate(FAKE_SPEC, {"max_agents": "many"})

    def test_duration_shapes(self):
        assert knobs_spec.validate(FAKE_SPEC, {"stall": "10m"}) == {"stall": "10m"}
        assert knobs_spec.validate(FAKE_SPEC, {"stall": "600"}) == {"stall": 600}
        assert knobs_spec.validate(FAKE_SPEC, {"stall": 0}) == {"stall": 0}
        with pytest.raises(ValueError, match="stall"):
            knobs_spec.validate(FAKE_SPEC, {"stall": "ten minutes"})

    def test_unknown_name_is_rejected(self):
        with pytest.raises(ValueError, match="unknown knob"):
            knobs_spec.validate(FAKE_SPEC, {"nope": 1})

    def test_none_is_empty(self):
        assert knobs_spec.validate(FAKE_SPEC, None) == {}


class TestSpawnArgs:
    def test_table_order_and_bool_pairs(self):
        knobs = {"auditor": True, "model": "m", "stall": "10m", "thinking": "on"}
        assert knobs_spec.spawn_args(FAKE_SPEC, knobs) == [
            "--model",
            "m",
            "--thinking",
            "on",
            "--stall",
            "10m",
            "--auditor",
        ]
        assert knobs_spec.spawn_args(FAKE_SPEC, {"auditor": False}) == ["--no-auditor"]

    def test_unknown_and_missing_are_skipped(self):
        assert knobs_spec.spawn_args(FAKE_SPEC, {"ghost": 1, "model": None}) == []
        assert knobs_spec.spawn_args(FAKE_SPEC, None) == []

    def test_model_only_table_passes_just_the_model(self):
        spec = tuple(knobs_spec._MODEL_ONLY)
        assert not knobs_spec.is_full(spec) and knobs_spec.is_full(FAKE_SPEC)
        assert knobs_spec.spawn_args(spec, {"model": "m", "thinking": "on"}) == [
            "--model",
            "m",
        ]
        with pytest.raises(ValueError):  # 구버전엔 모델 말고는 받을 노브가 없다
            knobs_spec.validate(spec, {"thinking": "on"})


class TestLoad:
    """`load` 는 autouse 픽스처가 가짜로 바꾼다 — 여기서는 원본을 직접 부른다."""

    def _real(self):
        return _REAL_LOAD

    def test_missing_binary_is_model_only(self, tmp_path):
        spec = self._real()(str(tmp_path / "no-such-agent-cli"))
        assert [k["name"] for k in spec] == ["model"] and not knobs_spec.is_full(spec)

    def test_old_binary_without_the_subcommand_is_model_only(self, tmp_path):
        fake = tmp_path / "old-cli"
        fake.write_text("#!/bin/sh\necho 'No such command knobs' >&2\nexit 2\n")
        fake.chmod(0o755)
        assert not knobs_spec.is_full(self._real()(str(fake)))

    def test_table_is_read_from_the_binary(self, tmp_path):
        import json

        fake = tmp_path / "cli"
        fake.write_text(
            "#!/bin/sh\ncat <<'EOF'\n" + json.dumps(list(FAKE_SPEC)) + "\nEOF\n"
        )
        fake.chmod(0o755)
        spec = self._real()(str(fake))
        assert [k["name"] for k in spec] == [k["name"] for k in FAKE_SPEC]

    def test_malformed_table_is_model_only(self, tmp_path):
        fake = tmp_path / "cli"
        fake.write_text('#!/bin/sh\necho \'[{"flag": "--x"}]\'\n')
        fake.chmod(0o755)
        assert not knobs_spec.is_full(self._real()(str(fake)))


@pytest.mark.skipif(shutil.which("agent-cli") is None, reason="agent-cli not installed")
class TestRealBinary:
    """설치된 agent-cli 와의 계약 — 표의 모양이 보드가 읽는 키를 가진다."""

    def test_installed_agent_cli_serves_a_full_table(self):
        out = subprocess.run(
            ["agent-cli", "--version"], capture_output=True, text=True, check=False
        )
        version = tuple(
            int(x) for x in (out.stdout + out.stderr).strip().split()[-1].split(".")[:2]
        )
        spec = _REAL_LOAD("agent-cli")
        if version < (10, 45):
            assert not knobs_spec.is_full(spec)
            return
        names = [k["name"] for k in spec]
        assert names[0] == "model" and {
            "thinking",
            "grammar",
            "auditor",
            "stall",
        } <= set(names)
        for k in spec:
            assert {"name", "flag", "kind", "choices", "default", "ui", "label"} <= set(
                k
            )
            assert k["kind"] in ("text", "choice", "bool", "number", "duration")
        assert knobs_spec.validate(
            spec, {"model": "m", "auditor": "on", "stall": "10m"}
        ) == {
            "model": "m",
            "auditor": True,
            "stall": "10m",
        }
