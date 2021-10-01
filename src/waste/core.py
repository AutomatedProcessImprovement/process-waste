import uuid
from pathlib import Path
from typing import Tuple, Optional

import pandas as pd
from pm4py.objects.conversion.log import converter as log_converter
from pm4py.objects.log.importer.xes import importer as xes_importer
from pm4py.objects.log.util import interval_lifecycle
from pm4py.statistics.concurrent_activities.pandas import get as concurrent_activities_get


def lifecycle_to_interval(log_path: Path) -> pd.DataFrame:
    log = xes_importer.apply(str(log_path))
    log_interval = interval_lifecycle.to_interval(log)
    event_log_interval = log_converter.apply(log_interval, variant=log_converter.Variants.TO_DATA_FRAME)
    return event_log_interval


def get_concurrent_activities(case: pd.DataFrame) -> list[Tuple]:
    def _preprocess_case(case: pd.DataFrame):
        # subtracting a microsecond from `time:timestamp` to avoid touching events to be concurrent ones
        case['time:timestamp'] = case['time:timestamp'] - pd.Timedelta('1 us')
        return case

    case = _preprocess_case(case)

    params = {concurrent_activities_get.Parameters.TIMESTAMP_KEY: "time:timestamp",
              concurrent_activities_get.Parameters.START_TIMESTAMP_KEY: "start_timestamp"}
    concurrent_activities = concurrent_activities_get.apply(case, parameters=params)
    result = [activities for activities in concurrent_activities]
    return result


def make_aliases_for_concurrent_activities(case: pd.DataFrame, activities: list[Tuple]) -> dict:
    aliases = {}  # {alias: {data: <concurrent events>, replacement: <united event>}}
    for names in activities:
        # extract concurrent activities data
        data = pd.DataFrame()
        for name in names:
            event = case[case['concept:name'] == name]
            data = data.append(event)

        # create an alias for activities
        alias_id = str(uuid.uuid4())
        replacement = data.iloc[0].copy()  # TODO: potential exception
        replacement['concept:name'] = alias_id
        replacement['start_timestamp'] = data['start_timestamp'].min()
        replacement['time:timestamp'] = data['time:timestamp'].max()
        replacement['org:resource'] = ':'.join(data['org:resource'])
        # NOTE: the rest of the data in this pseudo record is not relevant
        alias = {'data': data, 'replacement': replacement, 'previous_event': None, 'next_event': None}
        aliases[alias_id] = alias

    return aliases


def replace_concurrent_activities_with_aliases(case: pd.DataFrame, activities: list[Tuple],
                                               aliases: dict) -> pd.DataFrame:
    # Creating a new case dataframe with concurrent activities replaced by aliases to identify all sequential handoffs.

    # dropping concurrent activities
    case_with_aliases = case.copy()
    for names in activities:
        indices = case_with_aliases[case_with_aliases['concept:name'].isin(names)].index
        case_with_aliases.drop(index=indices, inplace=True)

    # adding concurrent activities replacements
    for alias_id in aliases:
        case_with_aliases = case_with_aliases.append(aliases[alias_id]['replacement'])

    case_with_aliases.reset_index(inplace=True, drop=True)
    case_with_aliases.sort_values(by='start_timestamp', inplace=True)
    return case_with_aliases


def identify_sequential_handoffs_locations(case: pd.DataFrame) -> pd.Series:
    resource_changed = case['org:resource'] != case.shift(-1)['org:resource']
    activity_changed = case['concept:name'] != case.shift(-1)['concept:name']
    handoff_occurred = resource_changed & activity_changed  # both conditions must be satisfied
    return handoff_occurred


def identify_sequential_handoffs(case: pd.DataFrame) -> pd.DataFrame:
    handoff_occurred = identify_sequential_handoffs_locations(case)

    # preparing a different dataframe for handoff reporting
    handoff = pd.DataFrame(
        columns=['source_activity', 'source_resource', 'destination_activity', 'destination_resource', 'duration'])
    handoff.loc[:, 'source_activity'] = case[handoff_occurred]['concept:name']
    handoff.loc[:, 'source_resource'] = case[handoff_occurred]['org:resource']
    handoff.loc[:, 'destination_activity'] = case[handoff_occurred].shift(-1)['concept:name']
    handoff.loc[:, 'destination_resource'] = case[handoff_occurred].shift(-1)['org:resource']
    handoff['duration'] = case[handoff_occurred].shift(-1)['start_timestamp'] - case[handoff_occurred]['time:timestamp']

    # dropping an event at the end which is always 'True'
    handoff.reset_index(drop=True, inplace=True)
    handoff.drop(handoff.tail(1).index, inplace=True)

    # filling in N/A with some values
    handoff['source_resource'] = handoff['source_resource'].fillna('NA')
    handoff['destination_resource'] = handoff['destination_resource'].fillna('NA')

    # calculating frequency of the handoffs with the same activities and resources
    handoff_with_frequency = pd.DataFrame(
        columns=['source_activity', 'source_resource', 'destination_activity', 'destination_resource', 'duration'])
    handoff_grouped = handoff.groupby(
        by=['source_activity', 'source_resource', 'destination_activity', 'destination_resource'])
    for group in handoff_grouped:
        pair, records = group
        # print(f"Source-destination pair: {pair}, frequency per case: {len(records)}")
        handoff_with_frequency = handoff_with_frequency.append(pd.Series({
            'source_activity': pair[0],
            'source_resource': pair[1],
            'destination_activity': pair[2],
            'destination_resource': pair[3],
            'duration': records['duration'].sum(),
            'frequency': len(records)
        }), ignore_index=True)

    return handoff_with_frequency


# NOTE: mutates aliases
def identify_concurrent_handoffs(case: pd.DataFrame, aliases: dict) -> Optional[pd.DataFrame]:
    # Coming back to concurrent activities to identify sequential to concurrent and concurrent to sequential handoffs.

    def _get_previous_and_next_events(case: pd.DataFrame, index: int) -> (
    Optional[pd.DataFrame], Optional[pd.DataFrame]):
        previous_event = None
        next_event = None
        if index != 0:
            previous_event = case.iloc[index - 1]
        if index != len(case) - 1:
            next_event = case.iloc[index + 1]
        return previous_event, next_event

    handoff_occurred = identify_sequential_handoffs_locations(case)

    potential_handoffs = case[handoff_occurred].copy()
    potential_handoffs.sort_values(by='start_timestamp', inplace=True)
    potential_handoffs.reset_index(inplace=True, drop=True)

    concurrent_handoffs = []

    for alias_id in aliases:
        index = potential_handoffs[potential_handoffs['concept:name'] == alias_id].index[0]
        previous_event, next_event = _get_previous_and_next_events(potential_handoffs, index)
        prev_handoffs = identify_concurrent_handoffs_left(previous_event, aliases[alias_id]['data'])
        next_handoffs = identify_concurrent_handoffs_right(next_event, aliases[alias_id]['data'])
        concurrent_handoffs.append(prev_handoffs)
        concurrent_handoffs.append(next_handoffs)

    if len(concurrent_handoffs) > 0:
        return pd.concat(concurrent_handoffs)
    return None


def identify_concurrent_handoffs_left(previous_event: pd.Series, concurrent_events: pd.DataFrame):
    all_handoffs = []
    for i in concurrent_events.index:
        sequence = pd.DataFrame([previous_event, concurrent_events.loc[i]])
        handoffs = identify_sequential_handoffs(sequence)
        all_handoffs.append(handoffs)
    return pd.concat(all_handoffs)


def identify_concurrent_handoffs_right(next_event: pd.Series, concurrent_events: pd.DataFrame):
    all_handoffs = []
    for i in concurrent_events.index:
        sequence = pd.DataFrame([concurrent_events.loc[i], next_event])
        handoffs = identify_sequential_handoffs(sequence)
        all_handoffs.append(handoffs)
    return pd.concat(all_handoffs)


def identify_handoffs(case: pd.DataFrame) -> Optional[pd.DataFrame]:
    activities = get_concurrent_activities(case)
    aliases = make_aliases_for_concurrent_activities(case, activities)
    case_with_aliases = replace_concurrent_activities_with_aliases(case, activities, aliases)
    sequential_handoffs = identify_sequential_handoffs(case_with_aliases)
    concurrent_handoffs = identify_concurrent_handoffs(case_with_aliases, aliases)

    # removing handoffs related to aliases
    for alias_id in aliases:
        index = sequential_handoffs[sequential_handoffs['source_activity'] == alias_id].index
        sequential_handoffs.drop(index=index, inplace=True)
        index = sequential_handoffs[sequential_handoffs['destination_activity'] == alias_id].index
        sequential_handoffs.drop(index=index, inplace=True)

    handoffs = []
    if sequential_handoffs is not None and not sequential_handoffs.empty:
        handoffs.append(sequential_handoffs)
    if concurrent_handoffs is not None and not concurrent_handoffs.empty:
        handoffs.append(concurrent_handoffs)
    if len(handoffs) > 1:
        return pd.concat([sequential_handoffs, concurrent_handoffs])  # TODO: reset index
    elif len(handoffs) == 1:
        return handoffs[0]
    return None


def join_handoffs(handoffs: list[pd.DataFrame]) -> pd.DataFrame:
    """Joins a list of handoffs summing up frequency and duration."""
    columns = ['source_activity', 'source_resource', 'destination_activity', 'destination_resource']
    grouped = pd.concat(handoffs).groupby(columns)
    result = pd.DataFrame(columns=columns)
    for pair_index, group in grouped:
        source_activity, source_resource, destination_activity, destination_resource = pair_index
        group_duration = group['duration'].sum()
        group_frequency = group['frequency'].sum()
        result = result.append({
            'source_activity': source_activity,
            'source_resource': source_resource,
            'destination_activity': destination_activity,
            'destination_resource': destination_resource,
            'duration_sum': group_duration,
            'frequency': group_frequency
        }, ignore_index=True)
    result.reset_index(drop=True, inplace=True)
    return result