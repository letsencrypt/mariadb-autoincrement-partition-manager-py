"""
Bootstrap a table that does not have sufficient partitions to determine rates
of change.
"""

from datetime import timedelta
import logging
import operator
import yaml

from partitionmanager.types import (
    ChangePlannedPartition,
    MaxValuePartition,
    NewPlannedPartition,
)
from partitionmanager.table_append_partition import (
    table_is_compatible,
    get_current_positions,
    get_partition_map,
    generate_sql_reorganize_partition_commands,
)
from .tools import iter_show_end

RATE_UNIT = timedelta(hours=1)
MINIMUM_FUTURE_DELTA = timedelta(hours=2)


def _override_config_to_map_data(conf):
    """Return an analog to get_partition_map from override data in conf"""
    return {
        "range_cols": [str(x) for x in conf.assume_partitioned_on],
        "partitions": [
            MaxValuePartition("p_assumed", count=len(conf.assume_partitioned_on))
        ],
    }


def write_state_info(conf, out_fp):
    """
    Write the state info for tables defined in conf to the provided file-like
    object.
    """
    log = logging.getLogger("write_state_info")

    log.info("Writing current state information")
    state_info = {"time": conf.curtime, "tables": dict()}
    for table in conf.tables:
        map_data = None
        if not conf.assume_partitioned_on:
            problem = table_is_compatible(conf.dbcmd, table)
            if problem:
                raise Exception(problem)
            map_data = get_partition_map(conf.dbcmd, table)
        else:
            map_data = _override_config_to_map_data(conf)

        positions = get_current_positions(conf.dbcmd, table, map_data["range_cols"])

        log.info(f'(Table("{table.name}"): {positions}),')
        state_info["tables"][str(table.name)] = positions

    yaml.dump(state_info, out_fp)


def _get_time_offsets(num_entries, first_delta, subseq_delta):
    """
    Construct a list of timedeltas of size num_entries of the form
    [ first_delta, subseq_delta, [subseq_delta...] ]
    """
    if num_entries < 1:
        raise ValueError("Must request at least one entry")

    time_units = [first_delta]
    while len(time_units) < num_entries:
        prev = time_units[-1]
        time_units.append(prev + subseq_delta)

    return time_units


def _plan_partitions_for_time_offsets(
    now_time, time_offsets, rate_of_change, ordered_current_pos, max_val_part
):
    """
    Return a list of PlannedPartitions, starting from now, corresponding to
    each supplied offset that will represent the positions then from the
    supplied current positions and the rate of change. The first planned
    partition will be altered out of the supplied MaxValue partition.
    """
    changes = list()
    for (i, offset), is_final in iter_show_end(enumerate(time_offsets)):
        increase = [x * offset / RATE_UNIT for x in rate_of_change]
        predicted_positions = [
            int(p + i) for p, i in zip(ordered_current_pos, increase)
        ]
        predicted_time = now_time + offset

        part = None
        if i == 0:
            part = (
                ChangePlannedPartition(max_val_part)
                .set_position(predicted_positions)
                .set_timestamp(predicted_time)
            )

        else:
            part = NewPlannedPartition().set_timestamp(predicted_time)

            if is_final:
                part.set_columns(len(predicted_positions))
            else:
                part.set_position(predicted_positions)

        changes.append(part)
    return changes


def calculate_sql_alters_from_state_info(conf, in_fp):
    """
    Using the config and the input yaml file-like object, return the SQL
    statements to bootstrap the tables in config that also have data in
    the input yaml as a dictionary of { Table -> list(SQL ALTER statements) }
    """
    log = logging.getLogger("calculate_sql_alters")

    log.info("Reading prior state information")
    prior_data = yaml.safe_load(in_fp)

    time_delta = (conf.curtime - prior_data["time"]) / RATE_UNIT
    if time_delta <= 0:
        raise ValueError(
            f"Time delta is too small: {conf.curtime} - "
            f"{prior_data['time']} = {time_delta}"
        )

    commands = dict()

    for table_name, prior_pos in prior_data["tables"].items():
        table = None
        for t in conf.tables:
            if t.name == table_name:
                table = t
        if not table:
            log.info(f"Skipping {table_name} as it is not in the current config")
            continue

        map_data = None

        if not conf.assume_partitioned_on:
            problem = table_is_compatible(conf.dbcmd, table)
            if problem:
                raise Exception(problem)
            map_data = get_partition_map(conf.dbcmd, table)
        else:
            map_data = _override_config_to_map_data(conf)

        current_positions = get_current_positions(
            conf.dbcmd, table, map_data["range_cols"]
        )

        ordered_current_pos = [
            current_positions[name] for name in map_data["range_cols"]
        ]
        ordered_prior_pos = [prior_pos[name] for name in map_data["range_cols"]]

        delta_positions = list(
            map(operator.sub, ordered_current_pos, ordered_prior_pos)
        )
        rate_of_change = list(map(lambda pos: pos / time_delta, delta_positions))

        max_val_part = map_data["partitions"][-1]
        if not isinstance(max_val_part, MaxValuePartition):
            log.error(f"Expected a MaxValue partition, got {max_val_part}")
            raise Exception("Unexpected part?")

        log.info(
            f"{table}, {time_delta:0.1f} hours, {ordered_prior_pos} - {ordered_current_pos}, "
            f"{delta_positions} pos_change, {rate_of_change}/hour"
        )

        part_duration = conf.partition_period
        if table.partition_period:
            part_duration = table.partition_period

        time_offsets = _get_time_offsets(
            1 + conf.num_empty, MINIMUM_FUTURE_DELTA, part_duration
        )

        changes = _plan_partitions_for_time_offsets(
            conf.curtime,
            time_offsets,
            rate_of_change,
            ordered_current_pos,
            max_val_part,
        )

        commands[table.name] = list(
            generate_sql_reorganize_partition_commands(table, changes)
        )

    return commands
