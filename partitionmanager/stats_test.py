import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from .stats import get_statistics, PrometheusMetrics
from .types import Table, MaxValuePartition
from .types_test import mkPPart


ts = datetime(1949, 1, 12, tzinfo=timezone.utc)


class TestStatistics(unittest.TestCase):
    def test_statistics_no_partitions(self):
        s = get_statistics(list(), ts, Table("no_parts"))
        self.assertEqual(s, {"partitions": 0})

    def test_statistics_single_unnamed_partition(self):
        s = get_statistics([MaxValuePartition("p_start", 1)], ts, Table("single_part"))
        self.assertEqual(s, {"partitions": 1})

    def test_statistics_single_partition(self):
        s = get_statistics(
            [MaxValuePartition("p_19480113", 1)], ts, Table("single_part")
        )
        self.assertEqual(
            s, {"partitions": 1, "time_since_newest_partition": timedelta(days=365)}
        )

    def test_statistics_two_partitions(self):
        s = get_statistics(
            [mkPPart("p_19480101", 42), MaxValuePartition("p_19490101", 1)],
            ts,
            Table("two_parts"),
        )
        self.assertEqual(
            s,
            {
                "partitions": 2,
                "time_since_newest_partition": timedelta(days=11),
                "time_since_oldest_partition": timedelta(days=377),
                "mean_partition_delta": timedelta(days=366),
                "max_partition_delta": timedelta(days=366),
            },
        )

    def test_statistics_weekly_partitions_year(self):
        parts = list()
        base = datetime(2020, 5, 20, tzinfo=timezone.utc)
        for w in range(0, 52):
            partName = f"p_{base + timedelta(weeks=w):%Y%m%d}"
            parts.append(mkPPart(partName, w * 1024))
        parts.append(MaxValuePartition(f"p_{base + timedelta(weeks=52):%Y%m%d}", 1))

        s = get_statistics(
            parts, base + timedelta(weeks=54), Table("weekly_partitions_year_retention")
        )
        self.assertEqual(
            s,
            {
                "partitions": 53,
                "time_since_newest_partition": timedelta(days=14),
                "time_since_oldest_partition": timedelta(days=378),
                "mean_partition_delta": timedelta(days=7),
                "max_partition_delta": timedelta(days=7),
            },
        )


class TestPrometheusMetric(unittest.TestCase):
    def test_rendering(self):
        exp = PrometheusMetrics()
        exp.add("name", "table_name", 42)

        f = StringIO()
        exp.render(f)
        self.assertEqual('partition_name{table="table_name"} 42\n', f.getvalue())

    def test_rendering_grouping(self):
        exp = PrometheusMetrics()
        exp.add("name", "table_name", 42)
        exp.add("second_metric", "table_name", 42)
        exp.add("name", "other_table", 42)

        f = StringIO()
        exp.render(f)
        self.assertEqual(
            """partition_name{table="table_name"} 42
partition_name{table="other_table"} 42
partition_second_metric{table="table_name"} 42
""",
            f.getvalue(),
        )

    def test_descriptions(self):
        exp = PrometheusMetrics()
        exp.add("name", "table_name", 42)
        exp.add("second_metric", "table_name", 42)
        exp.add("name", "other_table", 42)

        exp.describe(
            "second_metric", help_text="help for second_metric", type_name="type"
        )
        exp.describe("name", help_text="help for name", type_name="type")

        f = StringIO()
        exp.render(f)
        self.assertEqual(
            """# HELP partition_name help for name
# TYPE partition_name type
partition_name{table="table_name"} 42
partition_name{table="other_table"} 42
# HELP partition_second_metric help for second_metric
# TYPE partition_second_metric type
partition_second_metric{table="table_name"} 42
""",
            f.getvalue(),
        )
