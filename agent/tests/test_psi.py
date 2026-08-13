from spark_dash_agent.collectors.psi import PsiCollector, classify, parse_psi
from spark_dash_common.models import PsiState
from spark_dash_common.thresholds import PsiBands

IDLE = """some avg10=0.00 avg60=0.00 avg300=0.00 total=0
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
"""

UNDER_PRESSURE = """some avg10=23.45 avg60=12.30 avg300=4.10 total=123456789
full avg10=11.22 avg60=6.05 avg300=1.90 total=98765432
"""


def test_parse_psi_reads_both_rows():
    values = parse_psi(UNDER_PRESSURE)
    assert values["some_avg10"] == 23.45
    assert values["some_avg60"] == 12.30
    assert values["full_avg10"] == 11.22


def test_parse_psi_idle_is_all_zero():
    values = parse_psi(IDLE)
    assert values["some_avg10"] == 0.0
    assert values["full_avg10"] == 0.0


def test_parse_psi_ignores_unknown_rows():
    """A kernel adding a row must not break parsing."""
    values = parse_psi("some avg10=1.00 total=5\nweird avg10=99.00\n")
    assert values["some_avg10"] == 1.0
    assert "weird_avg10" not in values


def test_parse_psi_survives_garbage_values():
    values = parse_psi("some avg10=notanumber avg60=2.00 total=5\n")
    assert "some_avg10" not in values
    assert values["some_avg60"] == 2.0


def test_classify_bands():
    assert classify(0.0, 0.0) is PsiState.LOW
    assert classify(6.0, 0.0) is PsiState.MOD
    assert classify(25.0, 0.0) is PsiState.HIGH
    assert classify(60.0, 0.0) is PsiState.CRITICAL


def test_full_stall_outranks_some():
    """`full` means nothing is progressing, so it escalates faster."""
    assert classify(0.0, 2.0) is PsiState.MOD
    assert classify(0.0, 12.0) is PsiState.HIGH
    assert classify(0.0, 30.0) is PsiState.CRITICAL


def test_classify_takes_the_worse_signal():
    assert classify(60.0, 0.0) is PsiState.CRITICAL
    assert classify(0.0, 30.0) is PsiState.CRITICAL


def test_custom_bands_are_honored():
    bands = PsiBands(some_mod=50.0, some_high=60.0, some_critical=70.0)
    assert classify(10.0, 0.0, bands) is PsiState.LOW


def test_collector_returns_none_when_psi_unavailable(tmp_path):
    """No CONFIG_PSI (or not Linux) is 'nothing to report', not an error."""
    collector = PsiCollector(path=tmp_path / "missing")
    assert collector.collect() is None


def test_collector_reads_file(tmp_path):
    path = tmp_path / "memory"
    path.write_text(UNDER_PRESSURE)
    metrics = PsiCollector(path=path).collect()
    assert metrics is not None
    assert metrics.some_avg10 == 23.45
    assert metrics.state is PsiState.HIGH
