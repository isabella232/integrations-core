# -*- coding: utf-8 -*-
# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
import time
from contextlib import closing

import pymysql
from datadog import statsd

from datadog_checks.base import AgentCheck, is_affirmative
from datadog_checks.base.utils.db.sql import compute_sql_signature
from datadog_checks.base.utils.db.statement_metrics import StatementMetrics, apply_row_limits, is_dbm_enabled

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent

# monotonically increasing count metrics
METRICS = {
    'count': 'mysql.queries.count',
    'errors': 'mysql.queries.errors',
    'time': 'mysql.queries.time',
    'select_scan': 'mysql.queries.select_scan',
    'select_full_join': 'mysql.queries.select_full_join',
    'no_index_used': 'mysql.queries.no_index_used',
    'no_good_index_used': 'mysql.queries.no_good_index_used',
    'lock_time': 'mysql.queries.lock_time',
    'rows_affected': 'mysql.queries.rows_affected',
    'rows_sent': 'mysql.queries.rows_sent',
    'rows_examined': 'mysql.queries.rows_examined',
}

DEFAULT_METRIC_LIMITS = {k: (100000, 100000) for k in METRICS.keys()}


class MySQLStatementMetrics(object):
    """
    MySQLStatementMetrics collects database metrics per normalized MySQL statement
    """

    def __init__(self, instance, log):
        super(MySQLStatementMetrics, self).__init__()
        self.log = log
        self._state = StatementMetrics(self.log)
        self.is_enabled = instance.get('options', {}).get('collect_query_metrics', True)
        self.query_metric_limits = instance.get('options', {}).get('query_metric_limits', DEFAULT_METRIC_LIMITS)
        self.escape_query_commas_hack = instance.get('options', {}).get('escape_query_commas_hack', True)

    def collect_per_statement_metrics(self, instance, db, instance_tags):
        start_time = time.time()
        if not (is_dbm_enabled() and self.is_enabled):
            return []

        rows = self._query_summary_per_statement(db)
        keyfunc = lambda row: (row['schema'], row['digest'])
        rows = self._state.compute_derivative_rows(rows, METRICS.keys(), key=keyfunc)
        rows = apply_row_limits(rows, self.query_metric_limits, 'count', True, key=keyfunc)

        for row in rows:
            tags = list(instance_tags)
            if row['schema'] is not None:
                tags.append('schema:' + row['schema'])

            try:
                obfuscated_statement = datadog_agent.obfuscate_sql(row['query'])
            except Exception as e:
                self.log.warn("failed to obfuscate query '%s': %s", row['query'], e)
                continue

            tags.append('query_signature:' + compute_sql_signature(obfuscated_statement))

            if self.escape_query_commas_hack:
                obfuscated_statement = obfuscated_statement.replace(', ', '，').replace(',', '，')

            tags.append('query:' + obfuscated_statement[:200])

            for col, name in METRICS.items():
                value = row[col]
                self.log.debug("AgentCheck.count(%s, %s, tags=%s)", name, value, tags)
                instance.count(name, value, tags=tags)

        statsd.timing("dd.mysql.collect_per_statement_metrics.time", (time.time() - start_time) * 1000,
                      tags=instance_tags)

    def _query_summary_per_statement(self, db):
        """
        Collects per-statement metrics from performance schema. Because the statement sums are
        cumulative, the results of the previous run are stored and subtracted from the current
        values to get the counts for the elapsed period. This is similar to monotonic_count, but
        several fields must be further processed from the delta values.
        """

        sql_statement_summary ="""\
            SELECT `schema_name` as `schema`,
                `digest` as `digest`,
                `digest_text` as `query`,
                `count_star` as `count`,
                `sum_timer_wait` / 1000 as `time`,
                `sum_lock_time` / 1000 as `lock_time`,
                `sum_errors` as `errors`,
                `sum_rows_affected` as `rows_affected`,
                `sum_rows_sent` as `rows_sent`,
                `sum_rows_examined` as `rows_examined`,
                `sum_select_scan` as `select_scan`,
                `sum_select_full_join` as `select_full_join`,
                `sum_no_index_used` as `no_index_used`,
                `sum_no_good_index_used` as `no_good_index_used`
            FROM performance_schema.events_statements_summary_by_digest
            WHERE `digest_text` NOT LIKE 'EXPLAIN %'
            ORDER BY `count_star` DESC
            LIMIT 10000"""

        try:
            with closing(db.cursor(pymysql.cursors.DictCursor)) as cursor:
                cursor.execute(sql_statement_summary)

                rows = cursor.fetchall()
        except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
            self.warning("Statement summary metrics are unavailable at this time: %s", e)
            return []

        return rows