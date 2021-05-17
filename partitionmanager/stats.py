"""
Statistics-gathering tooling.
"""

import logging

from datetime import timedelta
import partitionmanager.tools
import partitionmanager.types


class PrometheusMetric:
    """Represents a single named metric for Prometheus"""

    def __init__(self, name, table, data):
        self.name = name
        self.table = table
        self.data = data


class PrometheusMetrics:
    """A set of metrics that can be rendered for Prometheus."""

    def __init__(self):
        self.metrics = dict()
        self.help = dict()
        self.types = dict()

    def add(self, name, table, data):
        """Record metric data representing the name and table."""
        if name not in self.metrics:
            self.metrics[name] = list()
        self.metrics[name].append(PrometheusMetric(name, table, data))

    def describe(self, name, help_text=None, type_name=None):
        """Add optional descriptive and type data for a given metric name."""
        self.help[name] = help_text
        self.types[name] = type_name

    def render(self, fp):
        """Write the collected metrics to the supplied file-like object.

        Follows the format specification:
        https://prometheus.io/docs/instrumenting/exposition_formats/
        """
        for n, metrics in self.metrics.items():
            name = f"partition_{n}"
            if n in self.help:
                print(f"# HELP {name} {self.help[n]}", file=fp)
            if n in self.types:
                print(f"# TYPE {name} {self.types[n]}", file=fp)
            for m in metrics:
                labels = [f'table="{m.table}"']
                print(f"{name}{{{','.join(labels)}}} {m.data}", file=fp)


def get_statistics(partitions, current_timestamp, table):
    """Return a dictionary of statistics about the supplied table's partitions."""
    log = logging.getLogger("get_statistics")
    results = {"partitions": len(partitions)}

    if not partitions:
        return results

    for p in partitions:
        if not partitionmanager.types.is_partition_type(p):
            log.warning(
                f"{table} get_statistics called with a partition list "
                + f"that included a non-Partition entry: {p}"
            )
            raise partitionmanager.types.UnexpectedPartitionException(p)

    head_part = None
    tail_part = partitions[-1]

    if not isinstance(tail_part, partitionmanager.types.MaxValuePartition):
        log.warning(
            f"{table} get_statistics called with a partition list tail "
            + f"that wasn't a MaxValuePartition: {tail_part}"
        )
        raise partitionmanager.types.UnexpectedPartitionException(tail_part)

    if tail_part.has_real_time and tail_part.timestamp():
        results["time_since_newest_partition"] = (
            current_timestamp - tail_part.timestamp()
        )

    # Find the earliest partition that is timestamped
    for p in partitions:
        if p.timestamp():
            head_part = p
            break

    if not head_part or head_part == tail_part:
        # For simple tables, we're done now.
        return results

    if head_part.timestamp():
        results["time_since_oldest_partition"] = (
            current_timestamp - head_part.timestamp()
        )

    if head_part.timestamp() and tail_part.timestamp():
        results["mean_partition_delta"] = (
            tail_part.timestamp() - head_part.timestamp()
        ) / (len(partitions) - 1)

    max_d = timedelta()
    for a, b in partitionmanager.tools.pairwise(partitions):
        if not a.timestamp() or not b.timestamp():
            log.debug(f"{table} had partitions that aren't comparable: {a} and {b}")
            continue
        d = b.timestamp() - a.timestamp()
        if d > max_d:
            max_d = d

    if max_d > timedelta():
        results["max_partition_delta"] = max_d

    return results
