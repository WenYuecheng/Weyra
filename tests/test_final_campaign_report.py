from benchmarks.final_campaign_report import distribution, paired


def test_fixed_denominator_keeps_missing_and_error_separate():
    report = distribution([{'status': 'pass'}, {'status': 'error'}], 8)
    assert report['pass'] == 1
    assert report['statuses'] == {'pass': 1, 'error': 1, 'not_run': 6}
    assert report['success_rate'] == 1 / 8


def test_pairing_excludes_infrastructure_errors_and_clusters_repeats():
    rows = [
        {'id': 'x', 'repeat': 0, 'variant': 'old', 'status': 'fail'},
        {'id': 'x', 'repeat': 0, 'variant': 'new', 'status': 'pass'},
        {'id': 'x', 'repeat': 1, 'variant': 'old', 'status': 'fail'},
        {'id': 'x', 'repeat': 1, 'variant': 'new', 'status': 'pass'},
        {'id': 'y', 'repeat': 0, 'variant': 'old', 'status': 'error'},
        {'id': 'y', 'repeat': 0, 'variant': 'new', 'status': 'pass'},
    ]
    report = paired(rows, 'old', 'new', repeats=True)
    assert report['complete_pairs'] == 2
    assert report['independent_tasks'] == 1
    assert report['candidate_only_success'] == 2
    assert report['difference_pp'] == 100
