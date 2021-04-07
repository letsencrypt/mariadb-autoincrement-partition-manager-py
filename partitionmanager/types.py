"""
Classes and types used across the Partition Manager
"""

import abc
import argparse
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


def retention_from_dict(r):
    """
    Process a dictionary, typically from YAML, which describes a table's
    retetntion period. Returns a timedelta or None, and raises an argparse
    error if the arguments are not understood.
    """
    for k, v in r.items():
        if k == "days":
            return timedelta(days=v)
        raise argparse.ArgumentTypeError(
            f"Unknown retention period definition: {k}={v}"
        )


class Table:
    """
    Represents enough information about a table to make partitioning decisions.
    """

    def __init__(self, name):
        self.name = SqlInput(name)
        self.retention = None
        self.partition_period = None

    def set_retention(self, ret):
        """
        Sets the retention period as a timedelta for this table
        """
        if not isinstance(ret, timedelta):
            raise ValueError("Must be a timedelta")
        self.retention = ret
        return self

    def set_partition_period(self, dur):
        """
        Sets the partition period as a timedelta for this table
        """
        if not isinstance(dur, timedelta):
            raise ValueError("Must be a timedelta")
        self.partition_period = dur
        return self

    def __str__(self):
        return f"Table {self.name}"


class SqlInput(str):
    """
    Class which wraps a string only if the string is safe to use within a
    single SQL statement.
    """

    valid_form = re.compile(r"^[A-Z0-9_-]+$", re.IGNORECASE)

    def __new__(cls, *args):
        if len(args) != 1:
            raise argparse.ArgumentTypeError(f"{args} is not a single argument")
        if not SqlInput.valid_form.match(args[0]):
            raise argparse.ArgumentTypeError(f"{args[0]} is not a valid SQL identifier")
        return super().__new__(cls, args[0])

    def __repr__(self):
        return str(self)


def toSqlUrl(urlstring):
    """
    Parse a sql://user:pass@host:port/schema URL and return the tuple.
    """
    try:
        urltuple = urlparse(urlstring)
        if urltuple.scheme.lower() != "sql":
            raise argparse.ArgumentTypeError(f"{urlstring} is not a valid sql://")
        if urltuple.path == "/" or urltuple.path == "":
            raise argparse.ArgumentTypeError(f"{urlstring} should include a db path")
        return urltuple
    except ValueError as ve:
        raise argparse.ArgumentTypeError(f"{urlstring} not valid: {ve}")


class DatabaseCommand(abc.ABC):
    """
    Abstract class which can run SQL commands and return the results in a
    minimal form.
    """

    @abc.abstractmethod
    def run(self, sql_cmd):
        """
        Run the sql, returning the results as a list of python-ized types, or
        raising an Exception
        """

    @abc.abstractmethod
    def db_name(self):
        """
        Return the current database name
        """


class Partition(abc.ABC):
    """
    Abstract class which represents a single, currently-defined SQL table
    partition. The subclasses represent: a partition with position information,
    PositionPartition; those which are the tail partition and catch IDs beyond
    the defined positions, MaxValuePartition; and a helper class,
    InstantPartition, which is only used temporarily and never stored.
    """

    @abc.abstractmethod
    def values(self):
        """
        Return a SQL partition value string.
        """

    @property
    @abc.abstractmethod
    def name(self):
        """
        Return the partition's name, which should generally represent the
        date that the partition begins to fill, of the form p_yyyymmdd
        """

    @property
    @abc.abstractmethod
    def num_columns(self):
        """
        Return the number of columns this partition represents
        """

    @property
    def has_time(self):
        """
        True if the partition has a timestamp, e.g. if timestamp() can be
        reasonably assumed to be non-None. Doesn't gaurantee, as this only
        allows for names to be of the form p_start or p_YYYY[MM[DD]].
        """
        if "start" in self.name:
            return False
        return True

    def timestamp(self):
        """
        Returns a datetime object representing this partition's
        date, if the partition is of the form "p_YYYYMMDD", otherwise
        returns None
        """

        if not self.has_time:
            # Gotta start somewhere, for partitions named things like
            # "p_start". This has the downside of causing abnormally-low
            # rate of change calculations, but they fall off quickly
            # for subsequent partitions
            return datetime(2021, 1, 1, tzinfo=timezone.utc)

        try:
            return datetime.strptime(self.name, "p_%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.strptime(self.name, "p_%Y%m").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.strptime(self.name, "p_%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

        return None

    def __repr__(self):
        return f"{type(self).__name__}<{str(self)}>"

    def __str__(self):
        return f"{self.name}: {self.values()}"


class PositionPartition(Partition):
    """
    A partition that may have positions assocated with it.
    """

    def __init__(self, name):
        self._name = name
        self.positions = list()

    @property
    def name(self):
        return self._name

    def set_position(self, positions):
        """
        Set the position list for this partition.
        """
        self.positions = [int(p) for p in positions]
        return self

    @property
    def num_columns(self):
        return len(self.positions)

    def values(self):
        return "(" + ", ".join([str(x) for x in self.positions]) + ")"

    def __lt__(self, other):
        if isinstance(other, MaxValuePartition):
            if len(self.positions) != other.num_columns:
                raise UnexpectedPartitionException(
                    f"Expected {len(self.positions)} columns but "
                    f"partition has {other.num_columns}."
                )
            return True
        other_positions = None
        if isinstance(other, list):
            other_positions = other
        elif isinstance(other, PositionPartition):
            other_positions = other.positions
        if not other_positions or len(self.positions) != len(other_positions):
            raise UnexpectedPartitionException(
                f"Expected {len(self.positions)} columns but partition has {other_positions}."
            )
        for v_mine, v_other in zip(self.positions, other_positions):
            if v_mine >= v_other:
                return False
        return True

    def __eq__(self, other):
        if isinstance(other, PositionPartition):
            return self.name == other.name and self.positions == other.positions
        return False


class MaxValuePartition(Partition):
    """
    A partition that lives at the tail of a partition list, saying
    all remaining values belong in this partition.
    """

    def __init__(self, name, count):
        self._name = name
        self.count = count

    @property
    def name(self):
        return self._name

    @property
    def num_columns(self):
        return self.count

    def values(self):
        return ", ".join(["MAXVALUE"] * self.count)

    def __lt__(self, other):
        """
        MaxValuePartitions are always greater than every other partition
        """
        if isinstance(other, list):
            if self.count != len(other):
                raise UnexpectedPartitionException(
                    f"Expected {self.count} columns but list has {len(other)}."
                )
            return False
        if isinstance(other, Partition):
            if self.count != other.num_columns:
                raise UnexpectedPartitionException(
                    f"Expected {self.count} columns but list has {other.num_columns}."
                )
            return False
        return ValueError()

    def __eq__(self, other):
        if isinstance(other, MaxValuePartition):
            return self.name == other.name and self.count == other.count
        return False


class InstantPartition(PositionPartition):
    """
    Represent a partition at the current moment, used for rate calculations
    as a stand-in that only exists for the purposes of the rate calculation
    itself.
    """

    def __init__(self, now, positions):
        super().__init__("Instant")
        self.instant = now
        self.positions = positions

    def timestamp(self):
        return self.instant


class PlannedPartition(abc.ABC):
    """
    An abstract class representing a partition this tool plans to emit. If
    the partition is an edit to an existing one, it will be the concrete type
    ChangePlannedPartition. For new partitions, it'll be NewPlannedPartition.
    """

    def __init__(self):
        self.num_columns = None
        self.positions = None
        self._timestamp = None
        self._important = False

    def set_timestamp(self, timestamp):
        """
        Set the timestamp to be used for the modified partition. This
        effectively changes the partition's name.
        """
        self._timestamp = timestamp.replace(hour=0, minute=0)
        return self

    def set_position(self, pos):
        """
        Set the position of this modified partition. If this partition
        changes an existing partition, the positions of both must have
        identical length.
        """
        if not isinstance(pos, list):
            raise ValueError()
        if self.num_columns is not None and len(pos) != self.num_columns:
            raise UnexpectedPartitionException(
                f"Expected {self.num_columns} columns but list has {len(pos)}."
            )
        self.positions = pos
        return self

    def set_important(self):
        """
        Indicate this is an important partition.
        """
        self._important = True
        return self

    def timestamp(self):
        """
        The timestamp of this partition.
        """
        return self._timestamp

    def important(self):
        """
        Whether this modified Partition is itself important enough to ensure
        commitment.
        """
        return self._important

    @property
    @abc.abstractmethod
    def has_modifications(self):
        """
        True if this partition modifies another partition.
        """

    def set_as_max_value(self):
        """
        Make this partition represent MAXVALUE and be represented by a
        MaxValuePartition by the as_partition method.
        """
        self.num_columns = len(self.positions)
        self.positions = None
        return self

    def as_partition(self):
        """
        Convert this from a Planned Partition to a Partition, which can then be
        rendered into a SQL ALTER.
        """
        if not self._timestamp:
            raise ValueError()
        if self.positions:
            return PositionPartition(f"p_{self._timestamp:%Y%m%d}").set_position(
                self.positions
            )
        return MaxValuePartition(f"p_{self._timestamp:%Y%m%d}", count=self.num_columns)

    def __repr__(self):
        return f"{type(self).__name__}<{str(self)}>"

    def __eq__(self, other):
        if isinstance(other, PlannedPartition):
            return (
                isinstance(self, type(other))
                and self.positions == other.positions
                and self.timestamp() == other.timestamp()
                and self.important() == other.important()
            )
        return False


class ChangePlannedPartition(PlannedPartition):
    """
    Represents modifications to a given Partition
    """

    def __init__(self, old_part):
        if not isinstance(old_part, Partition):
            raise ValueError()
        super().__init__()
        self.old = old_part
        self.num_columns = self.old.num_columns
        self._timestamp = self.old.timestamp()
        self._old_positions = (
            self.old.positions if isinstance(old_part, PositionPartition) else None
        )
        self.positions = self._old_positions

    @property
    def has_modifications(self):
        return (
            self.positions != self._old_positions
            or self.old.timestamp() is None
            and self._timestamp is not None
            or self._timestamp.date() != self.old.timestamp().date()
        )

    def __str__(self):
        imp = "[!!]" if self.important() else ""
        return f"{self.old} => {self.positions} {imp} {self._timestamp}"


class NewPlannedPartition(PlannedPartition):
    """
    Represents a wholly new Partition to be constructed
    """

    def __init__(self):
        super().__init__()
        self.set_important()

    def set_columns(self, count):
        """
        Set the number of columns needed to represent a position for this
        partition.
        """
        self.num_columns = count
        return self

    @property
    def has_modifications(self):
        return False

    def __str__(self):
        return f"Add: {self.positions} {self._timestamp}"


class MismatchedIdException(Exception):
    """
    Raised if the partition map doesn't use the primary key as its range id.
    """


class TruncatedDatabaseResultException(Exception):
    """
    Raised if the XML schema truncated over a subprocess interaction
    """


class DuplicatePartitionException(Exception):
    """
    Raise if a partition being created already exists.
    """


class UnexpectedPartitionException(Exception):
    """
    Raised when the partition map is unexpected.
    """


class TableInformationException(Exception):
    """
    Raised when the table's status doesn't include the information we need.
    """


class NoEmptyPartitionsAvailableException(Exception):
    """
    Raised if no empty partitions are available to safely modify.
    """
