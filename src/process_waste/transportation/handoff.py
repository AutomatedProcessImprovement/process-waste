from typing import Dict, Optional

import click
import pandas as pd

from process_waste import core


def identify(log: pd.DataFrame, parallel_activities: Dict[str, set], parallel_run=True) -> pd.DataFrame:
    click.echo(f'Handoff identification. Parallel run: {parallel_run}')
    result = core.identify_main(
        log=log,
        parallel_activities=parallel_activities,
        identify_fn_per_case=_identify_handoffs_per_case_and_make_report,
        join_fn=core.join_per_case_items,
        parallel_run=parallel_run)
    return result


def _identify_handoffs_per_case_and_make_report(case: pd.DataFrame, parallel_activities: Dict[str, set],
                                                case_id: str, enabled_on: bool = True) -> pd.DataFrame:
    # TODO: should ENABLED_TIMESTAMP_KEY be used?

    case = case.sort_values(by=[core.END_TIMESTAMP_KEY, core.START_TIMESTAMP_KEY]).copy()
    case.reset_index()

    strict_handoffs = _strict_handoffs_occurred(case, parallel_activities)
    self_handoffs = _self_handoffs_occurred(case, parallel_activities)
    potential_handoffs = pd.concat([strict_handoffs, self_handoffs])
    handoffs_index = potential_handoffs.index

    handoffs = _make_report(case, enabled_on, handoffs_index)

    handoffs_with_frequency = _calculate_frequency_and_duration(handoffs)

    # dropping edge cases with Start and End as an activity
    starts_ends_values = ['Start', 'End']
    starts_and_ends = (handoffs_with_frequency['source_activity'].isin(starts_ends_values)
                       & handoffs_with_frequency['source_resource'].isin(starts_ends_values)) \
                      | (handoffs_with_frequency['destination_activity'].isin(starts_ends_values)
                         & handoffs_with_frequency['destination_resource'].isin(starts_ends_values))
    handoffs_with_frequency = handoffs_with_frequency[starts_and_ends == False]

    # attaching case ID as additional information
    handoffs_with_frequency['case_id'] = case_id

    return handoffs_with_frequency


def _calculate_frequency_and_duration(handoffs: pd.DataFrame) -> pd.DataFrame:
    # calculating frequency per case of the handoffs with the same activities and resources
    columns = handoffs.columns.tolist()
    handoff_with_frequency = pd.DataFrame(columns=columns)
    handoff_grouped = handoffs.groupby(by=[
        'source_activity', 'source_resource', 'destination_activity', 'destination_resource'
    ])
    for group in handoff_grouped:
        pair, records = group
        handoff_with_frequency = pd.concat([handoff_with_frequency, pd.DataFrame({
            'source_activity': [pair[0]],
            'source_resource': [pair[1]],
            'destination_activity': [pair[2]],
            'destination_resource': [pair[3]],
            'duration': [records['duration'].sum()],
            'frequency': [len(records)],
            'handoff_type': [records['handoff_type'].iloc[0]]
        })], ignore_index=True)
    return handoff_with_frequency


def _make_report(case: pd.DataFrame, enabled_on: bool, handoffs_index: pd.Index) -> pd.DataFrame:
    # preparing a different dataframe for handoff reporting
    columns = ['source_activity', 'source_resource', 'destination_activity', 'destination_resource', 'duration',
               'handoff_type']
    handoffs = pd.DataFrame(columns=columns)
    for loc in handoffs_index:
        source = case.loc[loc]
        destination = case.loc[loc + 1]

        # duration calculation
        destination_start = destination[core.START_TIMESTAMP_KEY]
        if enabled_on:
            source_end = destination[core.ENABLED_TIMESTAMP_KEY]
        else:
            source_end = source[core.END_TIMESTAMP_KEY]
        duration = destination_start - source_end
        if duration < pd.Timedelta(0):
            duration = pd.Timedelta(0)

        # handoff type
        if source[core.RESOURCE_KEY] == destination[core.RESOURCE_KEY]:
            handoff_type = 'self'
        else:
            handoff_type = 'strict'

        # appending the handoff data
        handoff = pd.DataFrame({
            'source_activity': [source[core.ACTIVITY_KEY]],
            'source_resource': [source[core.RESOURCE_KEY]],
            'destination_activity': [destination[core.ACTIVITY_KEY]],
            'destination_resource': [destination[core.RESOURCE_KEY]],
            'duration': [duration],
            'handoff_type': [handoff_type]
        })
        handoffs = pd.concat([handoffs, handoff], ignore_index=True)

    # filling in N/A with some values
    handoffs['source_resource'] = handoffs['source_resource'].fillna('NA')
    handoffs['destination_resource'] = handoffs['destination_resource'].fillna('NA')
    return handoffs


def _strict_handoffs_occurred(case: pd.DataFrame, parallel_activities: Optional[Dict[str, set]] = None) -> pd.DataFrame:
    # TODO: should ENABLED_TIMESTAMP_KEY be used?

    # checking the main conditions for handoff to occur
    next_events = case.shift(-1)
    resource_changed = case[core.RESOURCE_KEY] != next_events[core.RESOURCE_KEY]
    activity_changed = case[core.ACTIVITY_KEY] != next_events[core.ACTIVITY_KEY]
    consecutive_timestamps = case[core.END_TIMESTAMP_KEY] <= next_events[core.START_TIMESTAMP_KEY]
    not_parallel = pd.Series(index=case.index, dtype=bool)
    prev_activities = case[core.ACTIVITY_KEY]
    next_activities = next_events[core.ACTIVITY_KEY]
    for (i, pair) in enumerate(zip(prev_activities, next_activities)):
        if pair[0] == pair[1]:
            not_parallel.iat[i] = False
            continue
        parallel_set = parallel_activities.get(pair[1], None) if parallel_activities else None
        if parallel_set and pair[0] in parallel_set:
            not_parallel.iat[i] = False
        else:
            not_parallel.iat[i] = True
    not_parallel = pd.Series(not_parallel)
    handoff_occurred = resource_changed & activity_changed & consecutive_timestamps & not_parallel
    handoffs = case[handoff_occurred]
    handoffs['handoff_type'] = 'strict'
    return handoffs


def _self_handoffs_occurred(case: pd.DataFrame, parallel_activities: Optional[Dict[str, set]] = None) -> pd.DataFrame:
    # TODO: should ENABLED_TIMESTAMP_KEY be used?

    # checking the main conditions for handoff to occur
    next_events = case.shift(-1)
    same_resource = case[core.RESOURCE_KEY] == next_events[core.RESOURCE_KEY]
    activity_changed = case[core.ACTIVITY_KEY] != next_events[core.ACTIVITY_KEY]
    consecutive_timestamps = case[core.END_TIMESTAMP_KEY] <= next_events[core.START_TIMESTAMP_KEY]
    not_parallel = pd.Series(index=case.index, dtype=bool)
    prev_activities = case[core.ACTIVITY_KEY]
    next_activities = next_events[core.ACTIVITY_KEY]
    for (i, pair) in enumerate(zip(prev_activities, next_activities)):
        if pair[0] == pair[1]:
            not_parallel.iat[i] = False
            continue
        parallel_set = parallel_activities.get(pair[1], None) if parallel_activities else None
        if parallel_set and pair[0] in parallel_set:
            not_parallel.iat[i] = False
        else:
            not_parallel.iat[i] = True
    not_parallel = pd.Series(not_parallel)
    handoff_occurred = same_resource & activity_changed & consecutive_timestamps & not_parallel
    handoffs = case[handoff_occurred]
    handoffs['handoff_type'] = 'self'
    return handoffs