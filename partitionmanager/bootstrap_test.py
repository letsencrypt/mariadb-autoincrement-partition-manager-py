import io
import unittest
import yaml
from datetime import datetime, timedelta

from .bootstrap import (
    _get_time_offsets,
    calculate_sql_alters_from_state_info,
    write_state_info,
)
from .cli import Config
from .types import DatabaseCommand, Table, SqlInput


class MockDatabase(DatabaseCommand):
    def __init__(self):
        self.response = []
        self.num_queries = 0

    def run(self, cmd):
        self.num_queries += 1

        if "CREATE_OPTIONS" in cmd:
            return [{"CREATE_OPTIONS": "partitioned"}]

        if "SHOW CREATE TABLE" in cmd:
            return [
                {
                    "Create Table": """CREATE TABLE `burgers` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`),
) ENGINE=InnoDB AUTO_INCREMENT=150 DEFAULT CHARSET=utf8
 PARTITION BY RANGE (`id`)
(PARTITION `p_start` VALUES LESS THAN MAXVALUE ENGINE = InnoDB)"""
                }
            ]

        if "SELECT" in cmd:
            return [{"id": 150}]

        return self.response

    def db_name(self):
        return SqlInput("the-database")


class TestBootstrapTool(unittest.TestCase):
    def test_writing_state_info(self):
        conf = Config()
        conf.curtime = datetime(2021, 3, 1)
        conf.dbcmd = MockDatabase()
        conf.tables = [Table("test")]

        out = io.StringIO()

        write_state_info(conf, out)

        written_yaml = yaml.safe_load(out.getvalue())

        self.assertEqual(
            written_yaml, {"tables": {"test": {"id": 150}}, "time": conf.curtime}
        )

    def test_get_time_offsets(self):
        self.assertEqual(
            _get_time_offsets(1, timedelta(hours=4), timedelta(days=30)),
            [timedelta(hours=4)],
        )

        self.assertEqual(
            _get_time_offsets(2, timedelta(hours=4), timedelta(days=30)),
            [timedelta(hours=4), timedelta(days=30, hours=4)],
        )

        self.assertEqual(
            _get_time_offsets(3, timedelta(hours=4), timedelta(days=30)),
            [
                timedelta(hours=4),
                timedelta(days=30, hours=4),
                timedelta(days=60, hours=4),
            ],
        )

    def test_read_state_info(self):
        conf_past = Config()
        conf_past.curtime = datetime(2021, 3, 1)
        conf_past.dbcmd = MockDatabase()
        conf_past.tables = [Table("test").set_partition_period(timedelta(days=30))]

        state_fs = io.StringIO()
        yaml.dump({"tables": {"test": {"id": 0}}, "time": conf_past.curtime}, state_fs)
        state_fs.seek(0)

        with self.assertRaises(ValueError):
            calculate_sql_alters_from_state_info(conf_past, state_fs)

        conf_now = Config()
        conf_now.curtime = datetime(2021, 3, 3)
        conf_now.dbcmd = MockDatabase()
        conf_now.tables = [Table("test").set_partition_period(timedelta(days=30))]

        state_fs.seek(0)
        x = calculate_sql_alters_from_state_info(conf_now, state_fs)
        self.assertEqual(
            x,
            {
                "test": [
                    "ALTER TABLE `test` REORGANIZE PARTITION `p_start` INTO "
                    "(PARTITION `p_20210303` VALUES LESS THAN (156), "
                    "PARTITION `p_20210402` VALUES LESS THAN (2406), "
                    "PARTITION `p_20210502` VALUES LESS THAN MAXVALUE);"
                ]
            },
        )
