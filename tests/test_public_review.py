from scripts.reports.public_review import review


def test_safe_format_review_preserves_primary_scores():
    assert review('{"answer":"B"}','B')['verdict']=='equivalent'
    assert review('\\boxed{\\frac{5}{3}}',r'$\\frac{5}{3}$')['verdict']=='equivalent'
    assert review(r'\\boxed{204\\pi i}',r'$204\\cdot \\pi \\cdot i$')['verdict']=='equivalent'
    assert review('10-pi^2','10 - pi^2')['verdict']=='equivalent'
    assert review('0.080','0.08')['verdict']=='equivalent'
    assert review('3/7','3/8')['verdict']=='different'


def test_unsafe_or_ambiguous_answer_remains_unreviewed():
    assert review('__import__("os").system("echo bad")','0')['verdict']=='unresolved'
    assert review('I first thought B, then C.','B')['verdict']=='unresolved'
    assert review('{"answer":"B","reason":"..."}','B')['verdict']=='unresolved'


def test_distinct_exact_decimals_are_not_promoted_by_float_rounding():
    assert review('1.00000000000000001', '1')['verdict'] == 'different'
    assert review('0.12345678901234567891', '0.12345678901234567892')['verdict'] == 'different'
    assert review('{"answer":1.00000000000000001}', '1')['verdict'] == 'different'
    assert review('1.00000000000000001-1', '0')['verdict'] == 'different'
    assert review('1.00000000000000001-1', '0.00000000000000001')['verdict'] == 'equivalent'
