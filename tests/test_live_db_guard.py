"""The conftest guard keeps the test suite away from the live Pueo DB (#947)."""

import os
import sqlite3

import pytest

import conftest


def _live_db() -> str:
    return os.path.join(conftest._PRODUCTION_STATE_ROOTS[0], "ha_agent_state.db")


class TestIsProductionDb:
    def test_live_db_path_is_flagged(self):
        assert conftest._is_production_db(_live_db())

    def test_file_uri_is_flagged(self):
        assert conftest._is_production_db(f"file:{_live_db()}?mode=ro")

    def test_tmp_path_is_not_flagged(self, tmp_path):
        assert not conftest._is_production_db(str(tmp_path / "x.db"))

    def test_memory_db_is_not_flagged(self):
        assert not conftest._is_production_db(":memory:")

    def test_sibling_dir_with_shared_prefix_is_not_flagged(self):
        root = conftest._PRODUCTION_STATE_ROOTS[0]
        assert not conftest._is_production_db(root + "-other/ha_agent_state.db")


class TestGuardedConnect:
    def test_connect_to_live_db_fails_the_test(self):
        with pytest.raises(pytest.fail.Exception, match="live Pueo database"):
            sqlite3.connect(_live_db())

    def test_guard_is_installed_once(self):
        # A second conftest import would wrap the guard in another guard and
        # move PUEO_CONFIG to a new temp dir.
        assert sqlite3.connect is conftest._guarded_sqlite_connect
        assert conftest._real_sqlite_connect is not conftest._guarded_sqlite_connect

    def test_connect_to_tmp_db_works(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "ok.db"))
        conn.execute("SELECT 1")
        conn.close()


class TestSessionPaths:
    def test_config_db_path_is_not_the_live_db(self):
        import config

        assert not conftest._is_production_db(config.DB_PATH)

    def test_pueo_config_is_not_the_live_config(self):
        # main() rewrites PUEO_CONFIG to each test's --config, so only assert
        # that it never resolves to the real platform config.
        live = os.path.realpath(
            os.path.join(conftest._pd.user_config_dir, "config.yaml")
        )
        assert os.path.realpath(os.environ["PUEO_CONFIG"]) != live
