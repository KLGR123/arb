"""Compare every frozen report value while permitting added diagnostic fields."""


def project(value, expected):
    if isinstance(expected, dict):
        return {key: project(value[key], item) for key, item in expected.items()}
    if isinstance(expected, list):
        assert len(value) == len(expected)
        return [project(item, old) for item, old in zip(value, expected)]
    return value


def legacy_report(report, expected):
    # Group-skip errors are new; all prior errors must still match exactly.
    report = dict(report)
    report['errors'] = [e for e in report['errors'] if e.get('stage') != 'group']
    report['summary'] = dict(report['summary'], errors=len(report['errors']))
    return project(report, expected)
