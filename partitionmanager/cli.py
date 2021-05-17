"""
Interface for running the partition manager from a CLI.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import logging
import traceback
import yaml

import partitionmanager.bootstrap
import partitionmanager.table_append_partition as pm_tap
import partitionmanager.types
import partitionmanager.stats
import partitionmanager.sql

PARSER = argparse.ArgumentParser(
    description="""
    In already-partitioned tables with an auto_increment key as the partition,
    add a new partition at the current auto_increment value.
"""
)

PARSER.add_argument(
    "--log-level",
    default=logging.INFO,
    type=lambda x: getattr(logging, x.upper()),
    help="Configure the logging level.",
)
PARSER.add_argument(
    "--prometheus-stats", type=Path, help="Path to produce a prometheus statistics file"
)
PARSER.add_argument(
    "--config", "-c", type=argparse.FileType("r"), help="Configuration YAML"
)

GROUP = PARSER.add_mutually_exclusive_group()
GROUP.add_argument("--mariadb", help="Path to mariadb command")
GROUP.add_argument(
    "--dburl",
    type=partitionmanager.types.toSqlUrl,
    help="DB connection url, such as sql://user:pass@10.0.0.1:3306/database",
)


class Config:
    """Configuration data that the rest of the tooling uses.

    Can be created from both an argparse object of command-line arguments, from
    a YAML file, both, and potentially be modified via unit tests.
    """

    def __init__(self):
        self.tables = set()
        self.dbcmd = None
        self.noop = False
        self.num_empty = 2
        self.curtime = datetime.now(tz=timezone.utc)
        self.partition_period = timedelta(days=30)
        self.prometheus_stats_path = None

    def from_argparse(self, args):
        """Populate this config from an argparse result.

        Overwrites only what is set by argparse.
        """
        if args.table:
            for n in args.table:
                self.tables.add(partitionmanager.types.Table(n))
        if args.dburl:
            self.dbcmd = partitionmanager.sql.IntegratedDatabaseCommand(args.dburl)
        elif args.mariadb:
            self.dbcmd = partitionmanager.sql.SubprocessDatabaseCommand(args.mariadb)
        if "days" in args and args.days:
            self.partition_period = timedelta(days=args.days)
            if self.partition_period <= timedelta():
                raise ValueError("Negative lifespan is not allowed")
        if "noop" in args:
            self.noop = args.noop
        if "prometheus_stats" in args:
            self.prometheus_stats_path = args.prometheus_stats

    def from_yaml_file(self, file):
        """Populate this config from the yaml in the file-like object supplied.

        Overwrites only what is set by the yaml.
        """
        data = yaml.safe_load(file)
        if "partitionmanager" not in data:
            raise TypeError(
                "Unexpected YAML format: missing top-level partitionmanager"
            )
        data = data["partitionmanager"]
        if "tables" not in data or not isinstance(data["tables"], dict):
            raise TypeError("Unexpected YAML format: no tables defined")
        if "noop" in data:
            self.noop = data["noop"]
        if "partition_period" in data:
            self.partition_period = partitionmanager.types.retention_from_dict(
                data["partition_period"]
            )
            if self.partition_period <= timedelta():
                raise ValueError("Negative lifespan is not allowed")
        if "num_empty" in data:
            self.num_empty = int(data["num_empty"])
        if not self.dbcmd:
            if "dburl" in data:
                self.dbcmd = partitionmanager.sql.IntegratedDatabaseCommand(
                    partitionmanager.types.toSqlUrl(data["dburl"])
                )
            elif "mariadb" in data:
                self.dbcmd = partitionmanager.sql.SubprocessDatabaseCommand(
                    data["mariadb"]
                )
        if not self.tables:  # Only load tables froml YAML if not supplied via args
            for key in data["tables"]:
                tab = partitionmanager.types.Table(key)
                tabledata = data["tables"][key]
                if isinstance(tabledata, dict) and "retention" in tabledata:
                    tab.set_retention(
                        partitionmanager.types.retention_from_dict(
                            tabledata["retention"]
                        )
                    )
                if isinstance(tabledata, dict) and "partition_period" in tabledata:
                    tab.set_partition_period(
                        partitionmanager.types.retention_from_dict(
                            tabledata["partition_period"]
                        )
                    )

                self.tables.add(tab)
        if "prometheus_stats" in data:
            self.prometheus_stats_path = Path(data["prometheus_stats"])


def config_from_args(args):
    """Helper that produces a Config from the arguments.

    Loads referenced YAML after the argparse completes.
    """
    conf = Config()
    conf.from_argparse(args)
    if args.config:
        conf.from_yaml_file(args.config)
    return conf


def all_configured_tables_are_compatible(conf):
    """Pre-flight test that all tables are compatible; returns True/False.

    Returns True only if all are compatible, otherwise logs errors and returns
    False.
    """
    problems = dict()
    for table in conf.tables:
        table_problems = pm_tap.get_table_compatibility_problems(conf.dbcmd, table)
        if table_problems:
            problems[table.name] = table_problems
            logging.error(f"Cannot proceed: {table} {table_problems}")

    return len(problems) == 0


def partition_cmd(args):
    """Runs do_partition on the config that results from the CLI arguments.

    Helper for argparse.
    """
    conf = config_from_args(args)
    return do_partition(conf)


SUBPARSERS = PARSER.add_subparsers(dest="subparser_name")
PARTITION_PARSER = SUBPARSERS.add_parser("maintain", help="maintain partitions")
PARTITION_PARSER.add_argument(
    "--noop",
    "-n",
    action="store_true",
    help="Don't attempt to commit changes, just print",
)
PARTITION_PARSER.add_argument(
    "--days", "-d", type=int, help="Lifetime of each partition in days"
)
PARTITION_PARSER.add_argument(
    "--table",
    "-t",
    type=partitionmanager.types.SqlInput,
    nargs="+",
    help="table names, overwriting config",
)
PARTITION_PARSER.set_defaults(func=partition_cmd)


def stats_cmd(args):
    """Runs do_stats on the config that results from the CLI arguments.

    Helper for argparse.
    """
    conf = config_from_args(args)
    return do_stats(conf)


STATS_PARSER = SUBPARSERS.add_parser("stats", help="get stats for partitions")
STATS_GROUP = STATS_PARSER.add_mutually_exclusive_group()
STATS_GROUP.add_argument(
    "--config", "-c", type=argparse.FileType("r"), help="Configuration YAML"
)
STATS_GROUP.add_argument(
    "--table",
    "-t",
    type=partitionmanager.types.SqlInput,
    nargs="+",
    help="table names, overwriting config",
)
STATS_PARSER.set_defaults(func=stats_cmd)


def bootstrap_cmd(args):
    """Runs bootstrap actions on the config that results from the CLI arguments.

    Helper for argparse.
    """
    conf = config_from_args(args)

    if args.outfile:
        partitionmanager.bootstrap.write_state_info(conf, args.outfile)

    if args.infile:
        return partitionmanager.bootstrap.calculate_sql_alters_from_state_info(
            conf, args.infile
        )

    return {}


BOOTSTRAP_PARSER = SUBPARSERS.add_parser(
    "bootstrap",
    help="bootstrap partitions that haven't been used with this tool before",
)
BOOTSTRAP_GROUP = BOOTSTRAP_PARSER.add_mutually_exclusive_group()
BOOTSTRAP_GROUP.add_argument(
    "--in", "-i", dest="infile", type=argparse.FileType("r"), help="input YAML"
)
BOOTSTRAP_GROUP.add_argument(
    "--out", "-o", dest="outfile", type=argparse.FileType("w"), help="output YAML"
)
BOOTSTRAP_PARSER.add_argument(
    "--table",
    "-t",
    type=partitionmanager.types.SqlInput,
    nargs="+",
    help="table names, overwriting config",
)
BOOTSTRAP_PARSER.set_defaults(func=bootstrap_cmd)


def do_partition(conf):
    """Produces SQL statements to manage partitions per the supplied configuration.

    If the configuration does not set the noop flag, this runs those statements
    as well.
    """
    log = logging.getLogger("partition")
    if conf.noop:
        log.info("No-op mode")

    # Preflight
    if not all_configured_tables_are_compatible(conf):
        return dict()

    metrics = partitionmanager.stats.PrometheusMetrics()
    metrics.describe(
        "alter_time_seconds",
        help_text="Time in seconds to complete the ALTER command",
        type_name="gauge",
    )

    all_results = dict()
    for table in conf.tables:
        try:
            map_data = pm_tap.get_partition_map(conf.dbcmd, table)

            duration = conf.partition_period
            if table.partition_period:
                duration = table.partition_period

            positions = pm_tap.get_current_positions(
                conf.dbcmd, table, map_data["range_cols"]
            )

            log.info(f"Evaluating {table} (duration={duration}) (pos={positions})")

            ordered_positions = [positions[col] for col in map_data["range_cols"]]

            sql_cmds = pm_tap.get_pending_sql_reorganize_partition_commands(
                table=table,
                partition_list=map_data["partitions"],
                current_positions=ordered_positions,
                allowed_lifespan=duration,
                num_empty_partitions=conf.num_empty,
                evaluation_time=conf.curtime,
            )

            if not sql_cmds:
                log.debug(f"{table} has no pending SQL updates.")
                continue

            composite_sql_command = "\n".join(sql_cmds)

            if conf.noop:
                all_results[table.name] = {"sql": composite_sql_command, "noop": True}
                log.info(f"{table} planned SQL: {composite_sql_command}")
                continue

            log.info(f"{table} running SQL: {composite_sql_command}")
            time_start = datetime.utcnow()
            output = conf.dbcmd.run(composite_sql_command)
            time_end = datetime.utcnow()

            all_results[table.name] = {"sql": composite_sql_command, "output": output}
            log.info(f"{table} results: {output}")
            metrics.add(
                "alter_time_seconds",
                table.name,
                (time_end - time_start).total_seconds(),
            )
        except partitionmanager.types.NoEmptyPartitionsAvailableException:
            log.warning(
                f"Unable to automatically handle {table}: No empty "
                "partition is available."
            )

    if conf.prometheus_stats_path:
        do_stats(conf, metrics)

    return all_results


def do_stats(conf, metrics=partitionmanager.stats.PrometheusMetrics()):
    """Populates a metrics object from the tables in the configuration."""
    if not all_configured_tables_are_compatible(conf):
        return dict()

    all_results = dict()
    for table in conf.tables:
        map_data = pm_tap.get_partition_map(conf.dbcmd, table)
        statistics = partitionmanager.stats.get_statistics(
            map_data["partitions"], conf.curtime, table
        )
        all_results[table.name] = statistics

    if conf.prometheus_stats_path:
        metrics.describe(
            "total", help_text="Total number of partitions", type_name="counter"
        )
        metrics.describe(
            "time_since_newest_partition_seconds",
            help_text="The age in seconds of the last partition for the table",
            type_name="gauge",
        )
        metrics.describe(
            "time_since_oldest_partition_seconds",
            help_text="The age in seconds of the first partition for the table",
            type_name="gauge",
        )
        metrics.describe(
            "mean_delta_seconds",
            help_text="Mean seconds between partitions",
            type_name="gauge",
        )
        metrics.describe(
            "max_delta_seconds",
            help_text="Maximum seconds between partitions",
            type_name="gauge",
        )

        for table, results in all_results.items():
            if "partitions" in results:
                metrics.add("total", table, results["partitions"])
            if "time_since_newest_partition" in results:
                metrics.add(
                    "time_since_newest_partition_seconds",
                    table,
                    results["time_since_newest_partition"].total_seconds(),
                )
            if "time_since_oldest_partition" in results:
                metrics.add(
                    "time_since_oldest_partition_seconds",
                    table,
                    results["time_since_oldest_partition"].total_seconds(),
                )
            if "mean_partition_delta" in results:
                metrics.add(
                    "mean_delta_seconds",
                    table,
                    results["mean_partition_delta"].total_seconds(),
                )
            if "max_partition_delta" in results:
                metrics.add(
                    "max_delta_seconds",
                    table,
                    results["max_partition_delta"].total_seconds(),
                )

        with conf.prometheus_stats_path.open(mode="w", encoding="utf-8") as fp:
            metrics.render(fp)

    return all_results


def main():
    """Start here."""
    args = PARSER.parse_args()
    logging.basicConfig(level=args.log_level)
    if "func" not in args:
        PARSER.print_help()
        return

    try:
        output = args.func(args)
        for key in output:
            print(f"{key}:")
            if isinstance(output[key], dict):
                for k, v in output[key].items():
                    print(f" {k}: {v}")
            elif isinstance(output[key], list):
                for v in output[key]:
                    print(f" - {v}")
            else:
                print(f" {output[key]}")
    except Exception as e:
        logging.warning(f"Couldn't complete command: {args.subparser_name}")
        logging.warning(traceback.format_exc())
        raise e


if __name__ == "__main__":
    main()
