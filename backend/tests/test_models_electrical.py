"""Testes dos modelos elétricos: FV, MPPT, bateria e cargas."""

from __future__ import annotations

import pytest

from smartenergy.config import load_config
from smartenergy.models.battery import BatteryModel
from smartenergy.models.loads import load_power
from smartenergy.models.mppt import MpptModel, mppt_output_from_input
from smartenergy.models.pv import PvModel


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def test_pv_power_and_cell_temperature(cfg):
    pv = PvModel.from_config(cfg)
    p_stc, derate, gamma = 50.0, 0.93, -0.004
    assert float(pv.power(1000.0, 25.0)) == pytest.approx(p_stc * derate)
    assert float(pv.power(500.0, 45.0)) == pytest.approx(p_stc * 0.5 * (1 + gamma * 20) * derate)
    assert float(pv.power(-5.0, 25.0)) == 0.0
    assert float(pv.cell_temperature(800.0, 25.0)) == pytest.approx(25.0 + (45.0 - 20.0))


def test_pv_operating_point(cfg):
    pv = PvModel.from_config(cfg)
    v, i = pv.operating_point(800.0, 40.0, 30.0, 30.0)
    assert v * i == pytest.approx(30.0)
    voc = pv.voc(800.0, 40.0)
    assert v == pytest.approx(voc * 18.0 / 21.6)
    v_c, i_c = pv.operating_point(800.0, 40.0, 30.0, 15.0)  # corte: tensão sobe em direção a Voc
    assert v < v_c < voc and v_c * i_c == pytest.approx(15.0)
    assert pv.operating_point(0.0, 20.0, 0.0, 0.0) == (0.0, 0.0)


def test_mppt_conversion(cfg):
    m = MpptModel.from_config(cfg)
    assert m.max_output(20.0) == pytest.approx(0.95 * 20.0 - 0.3)
    assert m.input_for_output(m.max_output(20.0), 20.0) == pytest.approx(20.0)
    assert m.input_for_output(5.0, 20.0) == pytest.approx((5.0 + 0.3) / 0.95)
    assert m.max_output(0.3) == 0.0 and m.input_for_output(1.0, 0.3) == 0.0
    assert float(mppt_output_from_input(10.0, 0.95, 0.3)) == pytest.approx(9.2)


def test_battery_ocv_and_inverse(cfg):
    b = BatteryModel.from_config(cfg, 0.5)
    assert b.ocv() == pytest.approx(13.05)
    assert b.ocv(0.55) == pytest.approx(13.075)
    assert b.soc_from_ocv(13.075) == pytest.approx(0.55)
    assert b.params.energy_wh == pytest.approx(76.8)


@pytest.mark.parametrize("p_w", [-40.0, -5.0, 0.0, 5.0, 30.0])
def test_battery_current_solves_power(cfg, p_w):
    b = BatteryModel.from_config(cfg, 0.5)
    i = b.current_for_power(p_w)
    v = b.ocv() - i * b.params.r_internal_ohm
    assert v * i == pytest.approx(p_w, abs=1e-9)
    assert (i > 0) == (p_w > 0)


def test_battery_charge_acceptance_taper(cfg):
    b = BatteryModel.from_config(cfg, 0.5)
    assert b.charge_acceptance_a(0.5) == pytest.approx(3.0)
    assert b.charge_acceptance_a(0.975) == pytest.approx(1.5)
    assert b.charge_acceptance_a(1.0) == pytest.approx(0.0)
    assert b.charge_acceptance_a(0.5, t_bat_c=-2.0) == 0.0


def test_battery_charge_regimes(cfg):
    b = BatteryModel.from_config(cfg, 0.5)
    free = b.solve_charge(10.0, 60.0)
    assert free.regime == "charge" and -free.p_w == pytest.approx(10.0)
    cc = b.solve_charge(100.0, 60.0)
    assert cc.regime == "charge_cc" and cc.i_a == pytest.approx(-3.0) and cc.limited
    assert cc.v_v == pytest.approx(b.ocv() + 3.0 * 0.04)
    b.soc = 0.98
    cv = b.solve_charge(100.0, 60.0)
    assert cv.regime == "charge_cv" and cv.v_v == pytest.approx(14.2)
    assert -cv.i_a == pytest.approx(3.0 * 0.02 / 0.05)


def test_battery_discharge_limits_and_integration(cfg):
    b = BatteryModel.from_config(cfg, 0.5)
    op = b.solve_discharge(20.0, 60.0)
    assert op is not None and op.v_v * op.i_a == pytest.approx(20.0)
    assert b.solve_discharge(200.0, 60.0) is None  # > 6 A: BMS abre
    b.soc = 0.001
    assert b.solve_discharge(20.0, 60.0) is None   # SOC ficaria negativo / V < UVP
    b.soc = 0.5
    b.integrate(6.0, 60.0)
    assert b.soc == pytest.approx(0.5 - 6.0 * 60.0 / (6.0 * 3600.0))
    b.integrate(-3.0, 60.0)
    assert b.soc == pytest.approx(0.5 - 6.0 * 60.0 / 21600.0 + 0.99 * 3.0 * 60.0 / 21600.0)


def test_load_power_model(cfg):
    res = cfg.load_by_key("res")
    fan = cfg.load_by_key("fan")
    assert load_power(res, 12.8) == pytest.approx(16.4)
    assert load_power(res, 14.2) == pytest.approx(16.4 * (14.2 / 12.8) ** 2)
    assert load_power(fan, 12.8, 0.6) == pytest.approx(3.6)
    assert load_power(fan, 13.0, 0.0) == 0.0 and load_power(fan, 0.0, 1.0) == 0.0
