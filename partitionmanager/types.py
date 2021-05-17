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


def is_partition_type(obj):
    """ True if the object inherits from a _Position. """
    return isinstance(obj, _Partition)


class _Partition(abc.ABC):
    """Abstract class which represents a existing table partition."""

    @abc.abstractmethod
    def values(self):
        """Return a SQL partition value string."""

    @property
    @abc.abstractmethod
    def name(self):
        """Name representing when the partition began to fill.

        Generally this will be of the form p_yyyymmdd, but sometimes partitions
        have names like p_initial, p_start, or any other valid SQL identifier.
        """

    @property
    @abc.abstractmethod
    def num_columns(self):
        """Return the number of columns this partition represents"""

    @property
    def has_real_time(self):
        """True if the partition has a non-synthetic timestamp.

        This should be used to determine whether timestamp() should be used for
        statistical purposes, as timestamp() generates a synthetic timestamp
        for rate-of-change calculations in corner-cases.
        """
        if "p_start" in self.name or not self.name.startswith("p_"):
            return False

        return self.timestamp() is not None

    def timestamp(self):
        """Returns datetime of this partition's date, or None.

        This returns the date from the partition's name if the partition is of
        the form "p_YYYYMMDD". If the name is "p_start", return a synthetic
        timestamp (be sure to use self.has_real_time before using for
        statistical purposes). Otherwise, returns None.
        """

        if not self.name.startswith("p_"):
            return None

        if "p_start" in self.name:
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


class PositionPartition(_Partition):
    """A partition that has positions assocated with it.

    Partitions are independent table segments, and each has a name and a list of
    positions. The positions-list is an ordered list of identifiers, matching
    the order of the table's partition-by statement when the table was created.
    """

    def __init__(self, name):
        self._name = name
        self._positions = list()

    @property
    def name(self):
        return self._name

    def set_position(self, positions):
        """Set the position list for this partition."""
        self._positions = [int(p) for p in positions]
        return self

    @property
    def positions(self):
        """Return the position list this partition represents"""
        return self._positions

    @property
    def num_columns(self):
        return len(self._positions)

    def values(self):
        return "(" + ", ".join([str(x) for x in self._positions]) + ")"

    def __lt__(self, other):
        if isinstance(other, MaxValuePartition):
            if len(self._positions) != other.num_columns:
                raise UnexpectedPartitionException(
                    f"Expected {len(self._positions)} columns but "
                    f"partition has {other.num_columns}."
                )
            return True
        other_positions = None
        if isinstance(other, list):
            other_positions = other
        elif isinstance(other, PositionPartition):
            other_positions = other.positions
        if not other_positions or len(self._positions) != len(other_positions):
            raise UnexpectedPartitionException(
                f"Expected {len(self._positions)} columns but partition has {other_positions}."
            )
        for v_mine, v_other in zip(self._positions, other_positions):
            if v_mine >= v_other:
                return False
        return True

    def __eq__(self, other):
        if isinstance(other, PositionPartition):
            return self.name == other.name and self._positions == other.positions
        return False


class MaxValuePartition(_Partition):
    """A partition that includes all remaining values.

    This kind of partition always resides at the tail of the partition list,
    and is defined as containing values up to the reserved keyword MAXVALUE.
    """

    def __init__(self, name, count):
        self._name = name
        self._count = count

    @property
    def name(self):
        return self._name

    @property
    def num_columns(self):
        return self._count

    def values(self):
        return ", ".join(["MAXVALUE"] * self._count)

    def __lt__(self, other):
        """MaxValuePartitions are always greater than every other partition."""
        if isinstance(other, list):
            if self._count != len(other):
                raise UnexpectedPartitionException(
                    f"Expected {self._count} columns but list has {len(other)}."
                )
            return False
        if is_partition_type(other):
            if self._count != other.num_columns:
                raise UnexpectedPartitionException(
                    f"Expected {self._count} columns but list has {other.num_columns}."
                )
            return False
        return ValueError()

    def __eq__(self, other):
        if isinstance(other, MaxValuePartition):
            return self.name == other.name and self._count == other.num_columns
        return False


class InstantPartition(PositionPartition):
    """Represent a partition at the current moment.

    Used for rate calculations as a stand-in that only exists for the purposes
    of the rate calculation itself.
    """

    def __init__(self, now, positions):
        super().__init__("Instant")
        self._instant = now
        self._positions = positions

    def timestamp(self):
        return self._instant


class _PlannedPartition(abc.ABC):
    """Represents a partition this tool plans to emit.

    The method as_partition will make this a concrete type for later evaluation.
    """

    def __init__(self):
        self._num_columns = None
        self._positions = None
        self._timestamp = None
        self._important = False

    def set_timestamp(self, timestamp):
        """Set the timestamp to be used for the modified partition.

        This effectively changes the partition's name.
        """
        self._timestamp = timestamp.replace(hour=0, minute=0)
        return self

    def set_position(self, pos):
        """Set the position of this modified partition.

        If this partition changes an existing partition, the positions of both
        must have identical length.
        """
        if not isinstance(pos, list):
            raise ValueError()
        if self.num_columns is not None and len(pos) != self.num_columns:
            raise UnexpectedPartitionException(
                f"Expected {self.num_columns} columns but list has {len(pos)}."
            )
        self._positions = pos
        return self

    def set_important(self):
        """Indicate this is an important partition."""
        self._important = True
        return self

    @property
    def positions(self):
        """Get the positions for this modified partition."""
        return self._positions

    def timestamp(self):
        """The timestamp of this partition."""
        return self._timestamp

    def important(self):
        """True if this Partition is important enough to ensure commitment."""
        return self._important

    @property
    @abc.abstractmethod
    def has_modifications(self):
        """True if this partition modifies another partition."""

    @property
    def num_columns(self):
        """Return the number of columns this partition represents."""
        return self._num_columns

    def set_as_max_value(self):
        """Represent this partition by MaxValuePartition from as_partition()"""
        self._num_columns = len(self._positions)
        self._positions = None
        return self

    def as_partition(self):
        """Return a concrete Partition that can be rendered into a SQL ALTER."""
        if not self._timestamp:
            raise ValueError()
        if self._positions:
            return PositionPartition(f"p_{self._timestamp:%Y%m%d}").set_position(
                self._positions
            )
        return MaxValuePartition(f"p_{self._timestamp:%Y%m%d}", count=self._num_columns)

    def __repr__(self):
        return f"{type(self).__name__}<{str(self)}>"

    def __eq__(self, other):
        if isinstance(other, _PlannedPartition):
            return (
                isinstance(self, type(other))
                and self.positions == other.positions
                and self.timestamp() == other.timestamp()
                and self.important() == other.important()
            )
        return False


class ChangePlannedPartition(_PlannedPartition):
    """Represents modifications to a Partition supplied during construction.

    Use the parent class' methods to alter this change.
    """

    def __init__(self, old_part):
        if not is_partition_type(old_part):
            raise ValueError()
        super().__init__()
        self._old = old_part
        self._num_columns = self._old.num_columns
        self._timestamp = self._old.timestamp()
        self._old_positions = (
            self._old.positions if isinstance(old_part, PositionPartition) else None
        )
        self._positions = self._old_positions

    @property
    def has_modifications(self):
        return (
            self._positions != self._old_positions
            or self._old.timestamp() is None
            and self._timestamp is not None
            or self._timestamp.date() != self._old.timestamp().date()
        )

    @property
    def old(self):
        """Get the partition to be modified"""
        return self._old

    def __str__(self):
        imp = "[!!]" if self.important() else ""
        return f"{self._old} => {self._positions} {imp} {self._timestamp}"


class NewPlannedPartition(_PlannedPartition):
    """Represents a wholly new Partition to be constructed.

    After construction, you must set the number of columns using set_columns
    before attempting to use this in a plan.
    """

    def __init__(self):
        super().__init__()
        self.set_important()

    def set_columns(self, count):
        """Set the number of columns needed to represent a position for this
        partition."""
        self._num_columns = count
        return self

    @property
    def has_modifications(self):
        return False

    def __str__(self):
        return f"Add: {self.positions} {self._timestamp}"


class MismatchedIdException(Exception):
    """ Raised if the partition map doesn't use the primary key as its range id."""


class TruncatedDatabaseResultException(Exception):
    """Raised if the XML schema truncated over a subprocess interaction"""


class DuplicatePartitionException(Exception):
    """Raise if a partition being created already exists."""


class UnexpectedPartitionException(Exception):
    """Raised when the partition map is unexpected."""


class TableInformationException(Exception):
    """Raised when the table's status doesn't include the information we need."""


class NoEmptyPartitionsAvailableException(Exception):
    """Raised if no empty partitions are available to safely modify."""
