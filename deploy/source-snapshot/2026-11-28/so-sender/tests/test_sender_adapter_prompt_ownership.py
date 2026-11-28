"""Regression tests for adapter-owned prompt directories.

Run from this snapshot with:
    python -m pytest deploy/source-snapshot/2026-11-28/so-sender/tests/test_sender_adapter_prompt_ownership.py
"""
import os
import sys
from pathlib import Path

import pytest

SENDER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SENDER_ROOT))

from sender_gateway.sender_adapter import (  # noqa: E402
    CODE_DEPENDENCY_UNAVAILABLE,
    ChatGPTSenderAdapter,
    SenderError,
)


@pytest.fixture
def cli(tmp_path):
    path = tmp_path / "fake_cli.py"
    path.write_text("pass\\n", encoding="utf-8")
    return str(path)


def test_caller_owned_root_survives_adapter_close(cli, tmp_path):
    shared = tmp_path / "prompts"
    shared.mkdir()
    marker = shared / "owned-by-caller.txt"
    marker.write_text("keep", encoding="utf-8")

    adapter = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    private_root = Path(adapter._prompt_root)
    prompt = Path(adapter._write_prompt("hello"))

    assert private_root.parent == shared
    assert prompt.parent == private_root
    adapter.close()

    assert shared.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not private_root.exists()


def test_two_adapters_can_close_independently_under_shared_root(cli, tmp_path):
    shared = tmp_path / "prompts"
    first = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    second = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    first_root = Path(first._prompt_root)
    second_root = Path(second._prompt_root)

    first.close()
    second_prompt = Path(second._write_prompt("still alive"))

    assert shared.is_dir()
    assert not first_root.exists()
    assert second_root.is_dir()
    assert second_prompt.read_text(encoding="utf-8") == "still alive"

    second.close()
    assert shared.is_dir()
    assert not second_root.exists()


def test_internally_created_root_is_cleaned_and_close_is_idempotent(cli):
    adapter = ChatGPTSenderAdapter(cli)
    owned_root = Path(adapter._prompt_root)
    adapter._write_prompt("temporary")
    assert owned_root.is_dir()

    adapter.close()
    adapter.close()

    assert not owned_root.exists()


def test_missing_prompt_directory_returns_actionable_sender_error(cli, tmp_path):
    shared = tmp_path / "prompts"
    adapter = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    import shutil

    shutil.rmtree(adapter._prompt_root)

    with pytest.raises(SenderError) as exc:
        adapter._write_prompt("cannot write")
    assert exc.value.code == CODE_DEPENDENCY_UNAVAILABLE
    assert "prompt storage unavailable" in exc.value.detail
    adapter.close()


def test_restart_style_second_adapter_can_write_after_first_closes(cli, tmp_path):
    shared = tmp_path / "prompts"
    first = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    first.close()

    second = ChatGPTSenderAdapter(cli, prompt_dir=str(shared))
    path = Path(second._write_prompt("after restart"))

    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "after restart"
    second.close()
    assert shared.is_dir()
