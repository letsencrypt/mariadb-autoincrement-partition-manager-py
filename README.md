[![Build Status](https://circleci.com/gh/letsencrypt/mariadb-sequential-partition-manager-py.svg?style=shield)](https://circleci.com/gh/letsencrypt/mariadb-sequential-partition-manager-py)
![Maturity Level: Beta](https://img.shields.io/badge/maturity-beta-blue.svg)

This tool partitions and manages MariaDB tables by sequential IDs.

This is primarily a mechanism for dropping large numbers of rows of data without using `DELETE` statements.

Adding partitions in the first place with InnoDB requires a full table copy. Otherwise, the `REORGANIZE PARTITION` command is fast only if operating on a partition that is empty, e.g., has no rows.

Similar tools:
* https://github.com/davidburger/gomypartition, intended for tables with date-based partitions
* https://github.com/yahoo/mysql_partition_manager, which is archived and in pure SQL

# Usage

```sh
 → git clone https://github.com/letsencrypt/mariadb-sequential-partition-manager-py.git
 → cd mariadb-sequential-partition-manager-py
 → python3 -m venv .venv
 → . .venv/bin/activate
 → python3 -m pip install .
 → tee /tmp/partman.conf.yml <<EOF
 partitionmanager:
  num_empty: 2
  partition_period:
      days: 90
  dburl: "sql://user:password@localhost3306:/test_db"
  tables:
    cats: {}
    dogs:
      partition_period:
        days: 30
  prometheus_stats: "/tmp/prometheus-textcollect-partition-manager.prom"
EOF
 → partition-manager --config /tmp/partman.conf.yml maintain --noop
INFO:root:No-op mode
INFO:partition:Evaluating Table dogs (duration=30 days, 0:00:00) (pos={'id': 150})
INFO:partition:Table dogs planned SQL: ALTER TABLE `dogs` REORGANIZE PARTITION `p_20201204` INTO (PARTITION `p_20210422` VALUES LESS THAN (221), PARTITION `p_20210522` VALUES LESS THAN MAXVALUE);

dogs:
 sql: ALTER TABLE `dogs` REORGANIZE PARTITION `p_20201204` INTO (PARTITION `p_20210422` VALUES LESS THAN (221), PARTITION `p_20210522` VALUES LESS THAN MAXVALUE);
 noop: True
```


## Hacking on this Project

```sh
 → git clone https://github.com/letsencrypt/mariadb-sequential-partition-manager-py.git
 → cd mariadb-sequential-partition-manager-py
 → python3 -m venv .venv
 → . .venv/bin/activate
 → python3 -m pip install --editable .
 → partition-manager --log-level=debug  \
    --mariadb test_tools/fake_mariadb.sh \
    maintain --noop --table tablename
DEBUG:root:Auto_Increment column identified as id
DEBUG:root:Partition range column identified as id
DEBUG:root:Found partition before = (100)
DEBUG:root:Found tail partition named p_20201204
INFO:root:No-op mode

ALTER TABLE `dbname`.`tablename` REORGANIZE PARTITION `p_20201204` INTO (PARTITION `p_20201204` VALUES LESS THAN (3101009), PARTITION `p_20210122` VALUES LESS THAN MAXVALUE);

```

# Configuration
You can use a yaml configuration file with the `--config` parameter of the form:
```yaml
partitionmanager:
  dburl: sql://user:password@localhost/db-name
  # or
  # mariadb: /usr/local/bin/mariadb
  partition_period:
    days: 7
  num_empty: 2

  tables:
    table1:
      retention:
        days: 60
    table2:
      partition_period:
        days: 30
    table3:
      retention:
        days: 14
    table4: {}
```

For tables which are either partitioned but not yet using this tool's schema, or which have no empty partitions, the `bootstrap` command can be useful for proposing alterations to run manually. Note that `bootstrap` proposes commands that are likely to require partial copies of each table, so likely they will require a maintenance period.

```sh
partition-manager --mariadb ~/bin/rootsql-dev-primary bootstrap --out /tmp/bootstrap.yml --table orders
INFO:write_state_info:Writing current state information
INFO:write_state_info:(Table("orders"): {'id': 9236}),

# wait some time
partition-manager --mariadb ~/bin/rootsql-dev-primary bootstrap --in /tmp/bootstrap.yml --table orders
INFO:calculate_sql_alters:Reading prior state information
INFO:calculate_sql_alters:Table orders, 24.0 hours, [9236] - [29236], [20000] pos_change, [832.706363653845]/hour
orders:
 - ALTER TABLE `orders` REORGANIZE PARTITION `p_20210405` INTO (PARTITION `p_20210416` VALUES LESS THAN (30901), PARTITION `p_20210516` VALUES LESS THAN (630449), PARTITION `p_20210615` VALUES LESS THAN MAXVALUE);

```

# Procedure

## Configuration Processing

- At start, if any configuration file specified as a CLI argument, read that configuration file to set all other values.
- Then, process all remaining command line arguments, overriding values loaded from the configuration file in case of conflicts.
- From those command-line arguments, determine whether to collect statistics `stats`, determine an initial partition layout `bootstrap`, or operate in the normal `maintain` mode.
- Use the configuration information as inputs to the required algorithm.

## "Maintain" Algorithm

The core algorithm is implemented in a method `get_pending_sql_reorganize_partition_commands` in `table_append_partition.py`. That algorithm is:

For a given table and that table's intended partition period, desired end-state is to have:
- All the existing partitions containing data,
- A configurable number of trailing partitions which contain no data, and
- An "active" partition currently being filled with data

To make it easier to manage, we give all the filled partitions a name to indicate the approximate date that partition began being filled with data. This date is approximate because once a partition contains data, it is no longer an instant `ALTER` operation to rename the partition, rather every contained row gets copied, so this tool predicts the date at which the new partition will become the "active" one.

Inputs:
- The table name
- The intended partition period
- The number of trailing partitions to keep
- The table's current partition list
- The table's partition id's current value(s)

Outputs:
- An intended partition list, changing only the empty partitions, or
- If no partitions can be reorganized, an error.

Procedure:
- Using the current values, split the partition list into two sub-lists: empty partitions, and non-empty partitions.
- If there are no empty partitions:
  - Raise an error and halt the algorithm.

- Perform a statistical regression using each non-empty partition to determine each partition's fill rate.
- Using each partition's fill rate and their age, predict the future partition fill rate.
- Create a new list of intended empty partitions.
- For each empty partition:
  - Predict the start-of-fill date using the partition's position relative to the current active partition, the current active partition's date, the partition period, and the future partition fill rate.
  - If the start-of-fill date is different than the partition's name, rename the partition.
  - Append the changed partition to the intended empty partition list.
- While the number of empty partitions is less than the intended number of trailing partitions to keep:
  - Predict the start-of-fill date for a new partition using the previous partition's date and the partition period.
  - Append the new partition to the intended empty partition list.
- Return the lists of non-empty partitions, the current empty partitions, and the post-algorithm intended empty partitions.

### "Maintain" Finalization

The results of the algorithm are converted into `ALTER` statements; if the user configured `--noop` they're emitted to console and the logs for each table. If not set to `--noop`, the application will execute the ALTERs at the database server and emit the results, including execution time as prometheus statistics if so configured.

## "Bootstrap" Algorithm

The bootstrap mode is a limited form of the "Maintain" Algorithm, using a temporary state file to determine rates-of-change. The bootstrap mode also does not limit itself to only affecting empty partitions, it can and will request changes that will prmopt row copies, in order to prepare a table for future use of the "Maintain" algorithm.

# TODOs

Lots:
[X] Support for tables with partitions across multiple columns.
[ ] A drop mechanism, for one. Initially it should take a retention period and log proposed `DROP` statements, not perform them.
[ ] Yet more tests, particularly live integration tests with a test DB.
