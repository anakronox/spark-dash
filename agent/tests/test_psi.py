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
    """Arguments are the 60-second averages — see `classify`."""
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
    # avg10 alone would read HIGH (23.45 >= 20); the band follows avg60, which
    # at 12.30/6.05 is still only MOD. See the next test for why.
    assert metrics.state is PsiState.MOD


def test_band_follows_avg60_not_a_10_second_spike(tmp_path):
    """The regression this replaced.

    avg10 spikes on any transient allocation burst, and classifying on it meant
    the band flickered without ever settling — measured over 24h on the GX10,
    avg10 held HIGH for 45 seconds and CRITICAL for 60. Alert rules wait 2-5
    minutes, so real pressure (full_avg10 peaked at 51%, twice its critical
    band) never fired a single alert.

    Here avg10 is deep in CRITICAL while avg60 has barely moved. The node is
    not in crisis, and the band must not say it is.
    """
    path = tmp_path / "memory"
    path.write_text(
        "some avg10=60.00 avg60=1.00 avg300=0.10 total=1\n"
        "full avg10=40.00 avg60=0.50 avg300=0.05 total=1\n"
    )
    metrics = PsiCollector(path=path).collect()
    assert metrics is not None
    assert metrics.some_avg10 == 60.0  # the spike is still reported…
    assert metrics.state is PsiState.LOW  # …it just doesn't set the band


def test_sustained_pressure_still_escalates(tmp_path):
    """Smoothing must not mean nothing ever reaches CRITICAL. Pressure that
    persists shows up in avg60, and that is what should escalate."""
    path = tmp_path / "memory"
    path.write_text(
        "some avg10=48.00 avg60=45.00 avg300=30.00 total=9\n"
        "full avg10=30.00 avg60=27.00 avg300=18.00 total=9\n"
    )
    metrics = PsiCollector(path=path).collect()
    assert metrics is not None
    # full_avg60 27.0 >= full_critical 25 — the GX10's real peak was 27.27.
    assert metrics.state is PsiState.CRITICAL
