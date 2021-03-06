COMMON_METADATA = {
    'kernelspec': {
        'display_name': 'Python 3',
        'language': 'python',
        'name': 'python3',
    },
    'language_info': {
        'name': '',
    },
}

def submission_metadata(key_to_version, extraction: bool):
    return {
        'judge_submission': {'exercises': key_to_version, 'extraction': extraction},
        **COMMON_METADATA,
    }

def master_metadata(exercise_key: str, autograde: bool, version: str, title=None, deadlines=None):
    if title is None:
        title = exercise_key
    if deadlines is None:
        deadlines = {}
    deadlines = {k: deadlines.get(k) for k in ('begins_at', 'opens_at', 'checks_at', 'closes_at', 'ends_at')}
    return {
        'judge_master': {
            'autograde': autograde,
            'deadlines': deadlines,
            'exercise_key': exercise_key,
            'title': title,
            'version': version,
        },
        **COMMON_METADATA,
    }

def master_metadata_version(metadata):
    return metadata.get('judge_master', {}).get('version', '')

def master_metadata_deadlines(metadata):
    deadlines = metadata.get('judge_master', {}).get('deadlines', {})
    return {k: deadlines.get(k) for k in ('begins_at', 'opens_at', 'checks_at', 'closes_at', 'ends_at')}
