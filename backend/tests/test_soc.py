"""Testes do estimador de SOC (docs/06 §1)."""

from __future__ import annotations

import pytest

from smartenergy.config import load_config
from smartenergy.estimation.soc import SocEstimator, ocv_inverse


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def curve(cfg):
    c = cfg.raw["battery"]["ocv_curve"]
    return c["soc"], c["v"]


# --------------------------------------------------------------------- OCV⁻¹
@pytest.mark.parametrize(
    "v, expected",
    [(13.1, 0.60), (12.4, 0.15), (13.025, 0.45), (11.6, 0.05), (10.0, 0.0), (9.0, 0.0),
     (13.6, 1.0), (14.5, 1.0), (13.4, 0.97)],
)
def test_ocv_inverse_interpolates_and_saturates(curve, v, expected):
    assert ocv_inverse(v, *curve) == pytest.approx(expected, abs=1e-12)


def test_ocv_inverse_rejects_bad_curve():
    with pytest.raises(ValueError):
        ocv_inverse(12.0, [0.0, 1.0], [10.0])


# ------------------------------------------------------------ inicialização
def test_init_persisted_uses_value_and_uncertainty(cfg):
    est = SocEstimator(cfg, initial_soc=0.42, initial_unc=0.07)
    assert (est.soc, est.unc, est.last_sync, est.valid) == (0.42, 0.07, "persisted", True)
    assert est.ah_since_sync == 0.0


def test_init_persisted_without_unc_defaults_to_sync_uncertainty(cfg):
    est = SocEstimator(cfg, initial_soc=0.5)
    assert est.unc == pytest.approx(cfg.soc_estimator.sync_uncertainty)


def test_init_persisted_has_priority_over_ocv(cfg):
    est = SocEstimator(cfg, initial_soc=0.3, initial_unc=0.1, v_init=13.3)
    assert est.soc == 0.3 and est.last_sync == "persisted"


def test_init_by_ocv_is_low_confidence(cfg):
    est = SocEstimator(cfg, v_init=13.1)
    assert est.soc == pytest.approx(0.6)
    assert est.unc == pytest.approx(cfg.soc_estimator.unknown_initial_uncertainty)
    assert est.last_sync == "ocv_init" and est.valid


def test_init_without_information(cfg):
    est = SocEstimator(cfg)
    assert est.last_sync == "none" and est.soc == 0.5 and est.unc == 0.5


# ------------------------------------------------------------- integração
def test_discharge_counts_coulombs(cfg):
    est = SocEstimator(cfg, initial_soc=0.5, initial_unc=0.05)
    est.update(3600.0, 2.0, 13.0)
    assert est.soc == pytest.approx(0.5 - 2.0 / 6.0)
    assert est.ah_since_sync == pytest.approx(2.0)
    assert est.unc == pytest.approx(0.05 + 0.01 * 2.0)
    assert est.since_sync_s == 3600.0


def test_charge_applies_coulombic_efficiency(cfg):
    est = SocEstimator(cfg, initial_soc=0.4, initial_unc=0.05)
    est.update(1800.0, -3.0, 13.3)
    assert est.soc == pytest.approx(0.4 + 0.99 * 1.5 / 6.0)
    assert est.ah_since_sync == pytest.approx(1.5)


@pytest.mark.parametrize("i, moves", [(0.004, False), (-0.0049, False), (0.0, False), (0.005, True),
                                      (-0.005, True), (0.0051, True)])
def test_current_deadband(cfg, i, moves):
    est = SocEstimator(cfg, initial_soc=0.5, initial_unc=0.05)
    est.update(3600.0, i, 13.1)
    assert (est.soc != 0.5) is moves
    assert (est.ah_since_sync > 0) is moves


def test_soc_is_clamped(cfg):
    est = SocEstimator(cfg, initial_soc=0.01, initial_unc=0.05)
    est.update(3600.0, 3.0, 12.5)
    assert est.soc == 0.0
    est = SocEstimator(cfg, initial_soc=0.99, initial_unc=0.05)
    est.update(3600.0, -3.0, 13.4)
    assert est.soc == 1.0


def test_uncertainty_is_capped(cfg):
    est = SocEstimator(cfg, initial_soc=0.9, initial_unc=0.45)
    est.update(3600.0, 6.0, 13.0)  # +6 Ah -> 0.45 + 0.06 > 0.5
    assert est.unc == 0.5


# ---------------------------------------------------- sincronização de cheio
def test_full_sync_after_hold_with_edge_event(cfg):
    est = SocEstimator(cfg, initial_soc=0.9, initial_unc=0.1)
    events = []
    for _ in range(11):  # 110 s < 120 s
        events += est.update(10.0, -0.25, 14.1)
    assert est.last_sync == "persisted" and events == []
    ev = est.update(10.0, -0.25, 14.1)  # 120 s
    assert [e[0] for e in ev] == ["SOC_SYNC_FULL"]
    assert est.soc == 1.0 and est.unc == pytest.approx(0.02) and est.ah_since_sync == 0.0
    assert est.last_sync == "full" and est.since_sync_s == 0.0
    # condição persiste: ressincroniza sem novo evento
    assert est.update(10.0, 0.25, 14.05) == []
    assert est.soc == 1.0 and est.ah_since_sync == 0.0


def test_full_sync_requires_tail_current_and_voltage(cfg):
    est = SocEstimator(cfg, initial_soc=0.9, initial_unc=0.1)
    for _ in range(20):
        est.update(10.0, -0.31, 14.2)       # corrente acima da cauda
    assert est.last_sync == "persisted"
    for _ in range(20):
        est.update(10.0, -0.1, 13.99)       # tensão abaixo
    assert est.last_sync == "persisted"
    for _ in range(6):
        est.update(10.0, -0.1, 14.0)        # 60 s na fronteira (V >= 14.0 vale)
    est.update(10.0, -1.0, 14.1)            # interrompe -> zera t_full
    for _ in range(11):
        est.update(10.0, -0.1, 14.0)
    assert est.last_sync == "persisted"
    est.update(10.0, -0.1, 14.0)
    assert est.last_sync == "full"


def test_full_sync_event_repeats_after_condition_breaks(cfg):
    est = SocEstimator(cfg, initial_soc=0.9, initial_unc=0.1)
    codes = []
    for _ in range(12):
        codes += [e[0] for e in est.update(10.0, -0.2, 14.1)]
    codes += [e[0] for e in est.update(10.0, -0.2, 13.0)]
    for _ in range(12):
        codes += [e[0] for e in est.update(10.0, -0.2, 14.1)]
    assert codes == ["SOC_SYNC_FULL", "SOC_SYNC_FULL"]


# ---------------------------------------------------- sincronização de vazio
def test_empty_sync_uses_compensated_voltage(cfg):
    est = SocEstimator(cfg, initial_soc=0.3, initial_unc=0.1)
    for _ in range(10):  # V = 11.75 <= 11.8, mas Vc = 11.75 + 2*0.04 = 11.83 > 11.8
        est.update(5.0, 2.0, 11.75)
    assert est.last_sync == "persisted"
    evs = []
    for _ in range(6):  # Vc = 11.70 + 0.04 = 11.74 -> 30 s
        evs += est.update(5.0, 1.0, 11.70)
    assert [e[0] for e in evs] == ["SOC_SYNC_EMPTY"]
    assert est.soc == pytest.approx(0.05) and est.last_sync == "empty"
    assert est.unc == pytest.approx(0.02)


def test_empty_sync_only_limits_from_above(cfg):
    est = SocEstimator(cfg, initial_soc=0.03, initial_unc=0.1)
    for _ in range(3):
        est.update(10.0, 0.0, 11.5)
    assert est.last_sync == "empty"
    assert est.soc == pytest.approx(0.03)


# ------------------------------------------------------- sensor inválido
def test_sensor_short_gap_suspends_without_invalidating(cfg):
    est = SocEstimator(cfg, initial_soc=0.5, initial_unc=0.05)
    for _ in range(3):  # 3 s: não é "mais de" 3 s
        est.update(1.0, None, None, sensor_ok=False)
    assert est.valid and est.soc == 0.5
    est.update(1.0, 0.0, 13.0)
    assert est.unc == pytest.approx(0.05)  # sem penalidade


def test_sensor_timeout_invalidates_and_return_adds_uncertainty(cfg):
    est = SocEstimator(cfg, initial_soc=0.5, initial_unc=0.05)
    for _ in range(4):
        est.update(1.0, 5.0, 13.0, sensor_ok=False)   # valores ignorados
    assert not est.valid
    assert est.soc == 0.5                               # integração suspensa
    est.update(1.0, 0.0, 13.0)
    assert est.valid and est.unc == pytest.approx(0.10)


def test_sensor_invalid_resets_sync_timers(cfg):
    est = SocEstimator(cfg, initial_soc=0.9, initial_unc=0.1)
    for _ in range(11):
        est.update(10.0, -0.2, 14.1)
    est.update(1.0, None, None, sensor_ok=False)
    est.update(10.0, -0.2, 14.1)
    assert est.last_sync == "persisted"


# ---------------------------------------------------------- sync manual
def test_manual_sync(cfg):
    est = SocEstimator(cfg, v_init=13.0)
    est.update(3600.0, 1.0, 13.0)
    est.sync(0.77)
    assert (est.soc, est.last_sync) == (0.77, "manual")
    assert est.unc == pytest.approx(cfg.soc_estimator.sync_uncertainty)
    assert est.ah_since_sync == 0.0
    est.sync(1.5)
    assert est.soc == 1.0


def test_snapshot_has_public_fields(cfg):
    snap = SocEstimator(cfg, initial_soc=0.5, initial_unc=0.05).snapshot()
    assert set(snap) == {"soc", "unc", "valid", "last_sync", "ah_since_sync", "since_sync_s"}
