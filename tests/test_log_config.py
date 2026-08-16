"""build_log_config — route uvicorn access logs to a rotating file."""

from __future__ import annotations

import logging.config

from agent_board.app import build_log_config


def test_access_log_goes_to_file(tmp_path):
    log = tmp_path / "board.log"
    cfg = build_log_config(log)
    assert cfg["handlers"]["access_file"]["filename"] == str(log)
    assert "RotatingFileHandler" in cfg["handlers"]["access_file"]["class"]
    # access logger uses ONLY the file handler (not the console)
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["access_file"]
    # startup/error still go to the console
    assert cfg["loggers"]["uvicorn.error"]["handlers"] == ["default"]


def test_config_is_dictconfig_loadable(tmp_path):
    # it must actually apply (formatters/handlers/loggers all resolve)
    logging.config.dictConfig(build_log_config(tmp_path / "board.log"))
    logging.getLogger("uvicorn.access").info("test %s", "line")
    assert (tmp_path / "board.log").exists()
