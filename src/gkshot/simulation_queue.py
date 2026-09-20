"""Choose a stage fairly without discarding completed screening rounds."""

DONE = {'complete', 'completed-with-errors'}


def next_stage(stages, blocked=()):
    """Return (stage id, cumulative round limit); None limit means refinement."""
    eligible = [s for s in stages if s['stage_id'] not in blocked and s.get('state') not in DONE]
    if any(s.get('state') == 'running' for s in stages):
        return None
    screening = [s for s in eligible if s.get('trials', 0) < 100]
    if screening:
        row = min(screening, key=lambda s: (s.get('trials', 0), s['stage_id']))
        return row['stage_id'], row.get('trials', 0) // 20 + 1
    if eligible:
        return min(s['stage_id'] for s in eligible), None
    return None
