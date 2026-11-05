"""Testes do supervisor de segurança (docs/06 §2)."""

from __future__ import annotations

import itertools

import pytest

from smartenergy.config import load_config
from smartenergy.ems.safety import CODE_PRIORITY, SafetySupervisor


@pytest.fixture(scope="module")
def cfg():
    return load_config()


class Driver:
    """Executa amostras com valores nominais e sobrescritas."""

    NOM = dict(v_bat=13.0, soc=0.5, soc_valid=True, v_bus=13.0, i_bus=1.0, t_bat=25.0)

    def __init__(self, cfg, boot_hold=False):
        self.sup = SafetySupervisor(cfg, boot_hold=boot_hold)
        self.t = 0.0
        self.events: list[str] = []

    def run(self, n=1, dt=0.5, **ov):
        st = None
        for _ in range(n):
            self.t += dt
            kw = {**self.NOM, **ov}
            st = self.sup.update(self.t, dt, kw["v_bat"], kw["soc"], kw["soc_valid"],
                                 v_bus=kw["v_bus"], i_bus=kw["i_bus"], t_bat=kw["t_bat"])
            self.events += [e[0] for e in st.events]
        return st


def test_nominal_is_ok(cfg):
    st = Driver(cfg).run(5)
    assert st.allowed and st.code == "OK" and st.active == ()


def test_simulation_style_call_without_bus_and_temperature(cfg):
    sup = SafetySupervisor(cfg)
    st = sup.update(1.0, 60.0, 13.1, 0.6, True)
    assert st.allowed and st.code == "OK"


# ------------------------------------------------------------------ BOOT
def test_boot_hold_until_valid_samples(cfg):
    d = Driver(cfg, boot_hold=True)
    n = cfg.safety.boot_settle_samples
    assert d.run(n - 1, dt=0.1).code == "BOOT"
    assert d.run(3, dt=0.1, v_bat=None, v_bus=None).code == "BOOT"  # inválidas não contam
    st = d.run(1, dt=0.1)
    assert st.code == "OK" and st.allowed


def test_reset_restores_boot(cfg):
    d = Driver(cfg, boot_hold=True)
    d.run(30, dt=0.1)
    d.sup.reset()
    assert d.run(1, dt=0.1).code == "BOOT"


# ------------------------------------------------------------------- LVD
def test_lvd_voltage_needs_delay_and_resets_on_recovery(cfg):
    d = Driver(cfg)
    assert d.run(9, v_bat=11.5).code == "OK"         # 4.5 s
    assert d.run(1, v_bat=11.7).code == "OK"         # interrompe
    assert d.run(9, v_bat=11.5).code == "OK"
    st = d.run(1, v_bat=11.5)                        # 5.0 s
    assert st.code == "LVD" and not st.allowed
    assert d.events == ["LVD_TRIP"]


def test_lvd_by_soc_is_immediate_and_needs_reconnect_conditions(cfg):
    d = Driver(cfg)
    assert d.run(1, soc=0.099).code == "LVD"
    assert d.run(1, soc=0.24).code == "LVD"                       # SOC < reconnect_min_soc
    assert d.run(1, soc=0.30, v_bat=12.89).code == "LVD"          # V < reconnect_v
    assert d.run(1, soc=0.25, v_bat=12.9).code == "OK"            # fronteiras inclusivas
    assert d.events == ["LVD_TRIP", "LVD_CLEAR"]


def test_invalid_soc_does_not_trip_but_allows_reconnect(cfg):
    d = Driver(cfg)
    assert d.run(3, soc=0.01, soc_valid=False).code == "OK"
    assert d.run(10, soc=0.01, soc_valid=False, v_bat=11.0).code == "LVD"
    assert d.run(1, soc=0.01, soc_valid=False, v_bat=13.0).code == "OK"


def test_lvd_uses_bus_voltage_when_bat_invalid(cfg):
    d = Driver(cfg)
    assert d.run(10, v_bat=None, v_bus=11.5).code == "LVD"
    assert d.run(1, v_bat=None, v_bus=13.0).code == "OK"


def test_lvd_timer_frozen_while_both_voltages_invalid(cfg):
    d = Driver(cfg)
    d.run(8, v_bat=11.5)                         # 4 s
    d.run(2, v_bat=None, v_bus=None)             # congela (sem SENSOR: 1 s)
    assert d.run(1, v_bat=11.5).code == "OK"     # 4.5 s
    assert d.run(1, v_bat=11.5).code == "LVD"    # 5.0 s


# ------------------------------------------------------------------- OVP
def test_ovp_trip_and_release_with_hysteresis(cfg):
    d = Driver(cfg)
    assert d.run(3, dt=0.25, v_bus=15.2).code == "OK"
    assert d.run(1, dt=0.25, v_bus=15.2).code == "OVP"
    assert d.run(8, dt=0.25, v_bus=14.8).code == "OVP"            # >= 15.0 - 0.3
    assert d.run(3, dt=0.25, v_bus=14.5).code == "OVP"
    assert d.run(1, dt=0.25, v_bus=14.5).code == "OK"
    assert d.events == ["OVP_TRIP", "OVP_CLEAR"]


def test_ovp_exactly_at_limit_does_not_trip(cfg):
    assert Driver(cfg).run(10, v_bus=15.0).code == "OK"


# -------------------------------------------------------------------- OC
def _trip_oc(d):
    return d.run(2, dt=0.125, i_bus=5.0)


def test_oc_delay_and_retry(cfg):
    d = Driver(cfg)
    assert d.run(1, dt=0.125, i_bus=5.0).code == "OK"
    assert d.run(1, dt=0.125).code == "OK"                         # pulso curto
    assert _trip_oc(d).code == "OC"
    assert d.run(5, dt=5.0, i_bus=0.0).code == "OC"
    assert d.run(1, dt=5.0, i_bus=0.0).code == "OK"                # 30 s
    assert d.events == ["OC_TRIP", "OC_RETRY"]


def test_oc_exactly_at_limit_does_not_trip(cfg):
    assert Driver(cfg).run(10, dt=0.125, i_bus=4.0).code == "OK"


def test_oc_lockout_after_max_trips_and_reset_faults(cfg):
    d = Driver(cfg)
    for _ in range(cfg.safety.overcurrent_max_retries - 1):
        assert _trip_oc(d).code == "OC"
        d.run(6, dt=5.0, i_bus=0.0)
    st = _trip_oc(d)
    assert st.code == "OC_LOCKOUT" and not st.allowed
    assert d.run(20, dt=100.0, i_bus=0.0).code == "OC_LOCKOUT"     # só rearme manual
    d.sup.reset_faults()
    st = d.run(1, dt=1.0, i_bus=0.0)
    assert st.code == "OK" and "FAULTS_RESET" in [e[0] for e in st.events]
    assert _trip_oc(d).code == "OC"                                # contagem zerada


def test_oc_count_resets_after_free_window(cfg):
    d = Driver(cfg)
    for _ in range(2):
        _trip_oc(d)
        d.run(6, dt=5.0, i_bus=0.0)
    d.run(6, dt=100.0)                                             # 600 s livres
    for _ in range(2):
        assert _trip_oc(d).code == "OC"
        d.run(6, dt=5.0, i_bus=0.0)
    assert _trip_oc(d).code == "OC_LOCKOUT"


# -------------------------------------------------------------------- OT
def test_ot_hysteresis(cfg):
    d = Driver(cfg, )
    assert d.run(1, t_bat=55.0).code == "OK"
    assert d.run(1, t_bat=55.1).code == "OT"
    assert d.run(1, t_bat=45.0).code == "OT"
    assert d.run(1, t_bat=None).code == "OT"
    assert d.run(1, t_bat=44.9).code == "OK"


# ---------------------------------------------------------------- SENSOR
def test_sensor_timeout_and_release(cfg):
    d = Driver(cfg)
    assert d.run(3, dt=1.0, v_bat=None, v_bus=None).code == "OK"   # 3 s: não é > 3
    st = d.run(1, dt=1.0, v_bat=None, v_bus=None)
    assert st.code == "SENSOR" and not st.allowed
    assert d.run(1, dt=1.0, v_bat=None).code == "OK"               # bus voltou
    assert d.events == ["SENSOR_FAIL", "SENSOR_OK"]


# ------------------------------------------------------------- prioridade
def test_priority_order_constant():
    assert CODE_PRIORITY == ("OC_LOCKOUT", "OC", "OVP", "OT", "SENSOR", "LVD", "BOOT")


@pytest.mark.parametrize("combo", [c for r in range(1, 4) for c in itertools.combinations(CODE_PRIORITY, r)])
def test_reported_code_follows_priority(cfg, combo):
    sup = SafetySupervisor(cfg)
    attr = {"OC_LOCKOUT": "oc_lockout", "OC": "oc_active", "OVP": "ovp_active", "OT": "ot_active",
            "SENSOR": "sensor_active", "LVD": "lvd_active", "BOOT": "boot_active"}
    for code in combo:
        setattr(sup, attr[code], True)
    expected = min(combo, key=CODE_PRIORITY.index)
    assert sup.active[0] == expected


def test_priority_end_to_end(cfg):
    d = Driver(cfg)
    assert d.run(10, v_bat=11.0).code == "LVD"
    assert d.run(1, v_bat=11.0, t_bat=60.0).code == "OT"
    assert d.run(4, dt=0.25, v_bat=11.0, t_bat=60.0, v_bus=15.5).code == "OVP"
    st = d.run(2, dt=0.125, v_bat=11.0, t_bat=60.0, v_bus=15.5, i_bus=6.0)
    assert st.code == "OC" and set(st.active) == {"OC", "OVP", "OT", "LVD"}
    st = d.run(5, dt=1.0, v_bat=None, v_bus=None, i_bus=0.0, t_bat=40.0)
    assert "SENSOR" in st.active and st.code == "OC"
