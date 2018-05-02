import datetime
from collections import defaultdict

import pandas as pd
from pyspark.sql.types import (
    LongType, MapType, StringType, StructField, StructType)

from mozaggregator.aggregator import (
    SCALAR_MEASURE_MAP, _aggregate_aggregates, _aggregate_ping,
    _sample_clients, _trim_payload)
from mozaggregator.db import histogram_revision_map
from moztelemetry.dataset import Dataset
from moztelemetry.histogram import Histogram


OUTPUT_PATH = 's3://net-mozaws-prod-us-west-2-pipeline-analysis/aggregates_poc/v1/'
SCHEMA = StructType([
    StructField('period', StringType(), False),
    StructField('aggregate_type', StringType(), False),
    StructField('submission_date', StringType(), False),
    StructField('channel', StringType(), False),
    StructField('version', StringType(), False),
    StructField('build_id', StringType(), True),
    StructField('application', StringType(), False),
    StructField('architecture', StringType(), False),
    StructField('os', StringType(), False),
    StructField('os_version', StringType(), False),
    StructField('metric', StringType(), False),
    StructField('key', StringType(), True),
    StructField('process_type', StringType(), False),
    StructField('count', LongType(), False),
    StructField('sum', LongType(), False),
    StructField(
        'histogram',
        MapType(StringType(), LongType(), False),
        False
    ),
])
BUILD_ID_CUTOFF_UNKNOWN = 45
BUILD_ID_CUTOFFS = {
    'release': 84,
    'esr': 84,
    'beta': 30,
    'aurora': 30,
    'nightly': 10,
}


def write_parquet(sc, aggregates, mode):
    build_id_agg = aggregates[0].flatMap(lambda row: _explode(row, 'build_id'))
    submission_date_agg = aggregates[1].flatMap(lambda row: _explode(row, 'submission_date'))
    df = sc.createDataFrame(build_id_agg, SCHEMA)
    df = df.union(sc.createDataFrame(submission_date_agg, SCHEMA))
    (df.repartition('metric', 'aggregate_type', 'period')
       .sortWithinPartitions(['channel', 'version', 'submission_date'])
       .write
       .partitionBy('metric', 'aggregate_type', 'period')
       .parquet(OUTPUT_PATH, mode=mode))


def _explode(row, aggregate_type):
    dimensions, metrics = row

    period = _period(dimensions[3] if aggregate_type == 'build_id' else dimensions[0])

    for k, v in metrics.iteritems():
        try:
            histogram = _get_complete_histogram(dimensions[1], k[0], v['histogram'])
        except KeyError:
            continue
        yield (period, aggregate_type,) + dimensions + k + (v['count'], v['sum'], histogram)


def _period(date_str):
    """
    Returns a period string given a string of "YYYYMMDD".

    Note: Make sure the return value is sortable as expected as a string, as queries
    against this will likely use `BETWEEN` or other comparisons.

    """
    return date_str[:6]


def _get_complete_histogram(channel, metric, values):
    revision = histogram_revision_map[channel]

    for prefix, labels in SCALAR_MEASURE_MAP.iteritems():
        if metric.startswith(prefix):
            histogram = pd.Series({int(k): v for k, v in values.iteritems()},
                                  index=labels).fillna(0)
            break
    else:
        histogram = Histogram(metric, {"values": values},
                              revision=revision).get_value(autocast=False)

    return {str(k): long(v) for k, v in histogram.to_dict().iteritems()}


def _telemetry_enabled(ping):
    try:
        return ping.get('environment', {}) \
                   .get('settings', {}) \
                   .get('telemetryEnabled', False)
    except Exception:
        return False


def _aggregate_metrics(pings, num_reducers=10000):
    trimmed = (
        pings.filter(_sample_clients)
             .map(_map_ping_to_dimensions)
             .filter(lambda x: x))
    build_id_aggregates = (
        trimmed.aggregateByKey(defaultdict(dict), _aggregate_ping,
                               _aggregate_aggregates, num_reducers))
    submission_date_aggregates = (
        build_id_aggregates.map(_map_build_id_key_to_submission_date_key)
                           .reduceByKey(_aggregate_aggregates))
    return build_id_aggregates, submission_date_aggregates


def _map_build_id_key_to_submission_date_key(aggregate):
    # This skips the build_id column and replaces it with `None`.
    return tuple(aggregate[0][:3] + (None,) + aggregate[0][4:]), aggregate[1]


def _map_ping_to_dimensions(ping):
    try:
        submission_date = ping["meta"]["submissionDate"]
        channel = ping["application"]["channel"]
        version = ping["application"]["version"].split('.')[0]
        build_id = ping["application"]["buildId"]
        application = ping["application"]["name"]
        architecture = ping["application"]["architecture"]
        os = ping["environment"]["system"]["os"]["name"]
        os_version = ping["environment"]["system"]["os"]["version"]

        if os == "Linux":
            os_version = str(os_version)[:3]

        try:
            build_id_as_date = datetime.datetime.strptime(build_id, '%Y%m%d%H%M%S')
        except ValueError:
            return None

        # Remove pings with build_id older than the specified cutoff days.
        cutoff = (
            datetime.date.today() -
            datetime.timedelta(days=BUILD_ID_CUTOFFS.get(channel, BUILD_ID_CUTOFF_UNKNOWN)))
        if build_id_as_date.date() <= cutoff:
            return None

        # TODO: Validate build_id string against the whitelist from build hub.

        subset = {}
        subset["payload"] = _trim_payload(ping["payload"])
        subset["payload"]["childPayloads"] = [
            _trim_payload(c) for c in ping["payload"].get("childPayloads", [])]

        # Note that some dimensions don't vary within a single submission
        # (e.g. channel) while some do (e.g. process type).
        # Dimensions that don't vary should appear in the submission key, while
        # the ones that do vary should appear within the key of a single metric.
        return (
            (submission_date, channel, version, build_id, application,
             architecture, os, os_version),
            subset
        )
    except KeyError:
        return None


def aggregate_metrics(sc, channels, submission_date, num_reducers=10000):
    """
    Returns the build-id and submission date aggregates for a given submission date.

    :param sc: A SparkContext instance
    :param channel: Either the name of a channel or a list/tuple of names
    :param submission-date: The submission date for which the data will be aggregated
    :param fraction: An approximative fraction of submissions to consider for aggregation
    """
    if not isinstance(channels, (tuple, list)):
        channels = [channels]

    channels = set(channels)
    source = 'telemetry-sample'
    where = {
        'appUpdateChannel': lambda x: x in channels,
        'submissionDate': submission_date,
        'sourceVersion': '4',
        'sampleId': 42,
    }

    pings = (Dataset.from_source(source)
                    .where(docType='main',
                           appName=lambda x: x != 'Fennec',
                           **where)
                    .records(sc)
                    .filter(_telemetry_enabled))

    fennec_pings = (Dataset.from_source(source)
                           .where(docType='saved_session',
                                  appName='Fennec',
                                  **where)
                           .records(sc))

    all_pings = pings.union(fennec_pings)
    return _aggregate_metrics(all_pings)


if __name__ == '__main__':
    from pyspark.sql import SparkSession

    sparkSession = SparkSession.builder.appName('tmo-poc').getOrCreate()

    date_start = datetime.date(2018, 3, 1)
    days = 31
    dates = [date_start + datetime.timedelta(n) for n in range(days)]

    for i, d in enumerate([d.strftime('%Y%m%d') for d in dates]):
        aggs = aggregate_metrics(sparkSession.sparkContext, 'nightly', d)
        write_parquet(sparkSession, aggs, 'overwrite' if i == 0 else 'append')


"""
When merging...

(df.repartition('metric', 'aggregate_type', 'period')
   .sortWithinPartitions(['channel', 'version', 'submission_date'])
   .write
   .partitionBy('metric', 'aggregate_type', 'period')
   .parquet('/home/rob/tmopoc/v2'))
"""
