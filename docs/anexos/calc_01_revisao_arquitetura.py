#!/usr/bin/env python3
"""
Calculos de apoio ao documento docs/01-revisao-arquitetura.md (ETAPA 1).

TODOS os resultados deste script sao ESTIMATIVAS de projeto (dimensionamento
preliminar), NAO medicoes. Valores de entrada marcados como PENDENTE no
config/system.yaml ou na pesquisa tecnica (docs/pesquisa/pesquisa-tecnica-2026-11.json)
continuam pendentes.

Uso (a partir da raiz do repositorio):
    .venv/bin/python docs/anexos/calc_01_revisao_arquitetura.py

Saida: tabelas em Markdown no stdout (copiadas para o documento 01).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "config" / "system.yaml").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# Painel de referencia: Renogy RNG-50P (unica ficha de 50 W/36 celulas com
# dados eletricos em fonte primaria na pesquisa). Faixa tipica de mercado
# tambem avaliada (fonte secundaria).
#   https://www.renogy.com/template/files/2015/06/RNG-50P-Specification.pdf
# ---------------------------------------------------------------------------
REF = dict(vmp=17.8, imp=2.8, voc=22.4, isc=2.95, beta_voc=-0.0030, alpha_isc=0.0004,
           gamma=-0.0044, noct=47.0, ns=36)
# Faixa tipica (fonte secundaria): Voc 21.5-22.4 V, beta_voc -0.29..-0.35 %/C
VOC_RANGE = (21.5, 22.4)
BETA_RANGE = (-0.0029, -0.0035)

K_B = 1.380649e-23
Q_E = 1.602176634e-19


def vt(tc):
    return K_B * (tc + 273.15) / Q_E


# ---------------------------------------------------------------------------
# Modelo de um diodo simplificado (Rsh -> infinito), ajustado aos pontos STC.
# I = Iph - I0*(exp((V + I*Rs)/a) - 1),  a = n*Ns*Vt
# ---------------------------------------------------------------------------
def fit_single_diode(p):
    """Ajusta n e Rs para reproduzir Voc, (Vmp, Imp) e dP/dV=0 no MPP em STC."""
    a_vt = p["ns"] * vt(25.0)
    best = None
    for n in np.linspace(0.8, 2.0, 1201):
        a = n * a_vt
        i0 = p["isc"] / (math.exp(p["voc"] / a) - 1.0)
        # Rs pela condicao de passagem pelo MPP
        arg = (p["isc"] - p["imp"]) / i0 + 1.0
        rs = (a * math.log(arg) - p["vmp"]) / p["imp"]
        if rs < 0:
            continue
        # dI/dV no MPP (implicito): dI/dV = -g/(1+g*Rs), g = I0/a*exp((V+IRs)/a)
        g = i0 / a * math.exp((p["vmp"] + p["imp"] * rs) / a)
        didv = -g / (1.0 + g * rs)
        resid = p["imp"] + p["vmp"] * didv  # dP/dV = I + V dI/dV = 0
        if best is None or abs(resid) < abs(best[0]):
            best = (resid, n, rs, i0)
    _, n, rs, _ = best
    return n, rs


def iv_current(v, tc, g, p, n, rs):
    """Corrente do modulo para tensao v (resolvida por Newton)."""
    dT = tc - 25.0
    a = n * p["ns"] * vt(tc)
    isc = p["isc"] * (g / 1000.0) * (1.0 + p["alpha_isc"] * dT)
    voc_t = p["voc"] * (1.0 + p["beta_voc"] * dT)  # Voc a 1000 W/m2 na temperatura tc
    isc_1000 = p["isc"] * (1.0 + p["alpha_isc"] * dT)
    i0 = isc_1000 / (math.exp(voc_t / a) - 1.0)
    i = isc
    for _ in range(100):
        e = math.exp((v + i * rs) / a)
        f = isc - i0 * (e - 1.0) - i
        df = -i0 * e * rs / a - 1.0
        step = f / df
        i -= step
        if abs(step) < 1e-10:
            break
    return max(i, 0.0)


def pv_curve(tc, g, p, n, rs, npts=4000):
    voc_guess = p["voc"] * (1.0 + p["beta_voc"] * (tc - 25.0)) + 2.0
    vs = np.linspace(0.0, voc_guess, npts)
    i = np.array([iv_current(v, tc, g, p, n, rs) for v in vs])
    return vs, i


def mpp(tc, g, p, n, rs):
    vs, i = pv_curve(tc, g, p, n, rs)
    pw = vs * i
    k = int(np.argmax(pw))
    voc = float(vs[np.argmax(i <= 1e-6)]) if np.any(i <= 1e-6) else float(vs[-1])
    return float(vs[k]), float(i[k]), float(pw[k]), voc


def tcell(ta, g, noct):
    return ta + (noct - 20.0) / 800.0 * g


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def f(x, nd=2):
    s = f"{x:.{nd}f}"
    return s.replace(".", ",")


def hours(intervals):
    tot = 0.0
    for a, b in intervals:
        ha = int(a[:2]) + int(a[3:]) / 60
        hb = int(b[:2]) + int(b[3:]) / 60
        tot += (hb - ha) if hb > ha else (24 - ha + hb)
    return tot


def overlap_h(intervals, w0, w1):
    """Horas de 'intervals' dentro da janela [w0,w1) (w1 pode cruzar meia-noite)."""
    def to_set(iv):
        mins = set()
        for a, b in iv:
            ma = int(a[:2]) * 60 + int(a[3:])
            mb = int(b[:2]) * 60 + int(b[3:])
            m = ma
            while m != mb:
                mins.add(m)
                m = (m + 1) % 1440
        return mins
    return len(to_set(intervals) & to_set([(w0, w1)])) / 60.0


def main():
    print("# Calculos de apoio — docs/01 (ESTIMATIVAS)\n")

    # ------------------------------------------------------------------ 1
    print("## 1. Voc em baixa temperatura (limite de tensao: INA226 36 V, MPPT 75 V)\n")
    rows = []
    for tc in (25, 10, 0, -5, -10):
        lo = VOC_RANGE[0] * (1 + BETA_RANGE[0] * (tc - 25))
        hi = VOC_RANGE[1] * (1 + BETA_RANGE[1] * (tc - 25))
        rows.append([tc, f(lo), f(hi), f(36 - hi), f(75 - hi)])
    print(md_table(["Tc (°C)", "Voc min (V)", "Voc max (V)", "folga INA226 36 V", "folga MPPT 75 V"], rows))

    # ------------------------------------------------------------------ 2
    n, rs = fit_single_diode(REF)
    print(f"\n## 2. Modelo de um diodo ajustado ao RNG-50P: n = {n:.3f}, Rs = {rs*1000:.0f} mOhm\n")
    v0, i0_, p0, voc0 = mpp(25, 1000, REF, n, rs)
    print(f"Verificacao STC: Vmp={v0:.2f} V Imp={i0_:.3f} A Pmp={p0:.2f} W Voc={voc0:.2f} V\n")
    v65, i65, p65, voc65 = mpp(65, 1000, REF, n, rs)
    gamma_model = (p65 / p0 - 1) / 40
    print(f"gamma emergente do modelo (25->65 C) = {gamma_model*100:.3f} %/C (ficha: -0,44 %/C)\n")

    ta_hot = CFG["simulation"]["weather"]["t_amb_max_c"]
    scen = [
        ("STC", 25.0, 1000),
        ("Manha fria, sol fraco", 15.0, 200),
        ("Dia ameno (Ta 20 °C, 1000 W/m²)", tcell(20, 1000, REF["noct"]), 1000),
        (f"Tarde quente (Ta {ta_hot:.0f} °C, 1000 W/m²)", tcell(ta_hot, 1000, REF["noct"]), 1000),
        ("Tarde muito quente (Ta 35 °C, 1000 W/m²)", tcell(35, 1000, REF["noct"]), 1000),
    ]
    print("## 3. Ponto de maxima potencia x janela do MPPT Victron (Vbat+1 V operacao; Vbat+5 V partida)\n")
    rows = []
    for name, tc, g in scen:
        vm, im, pm, voc = mpp(tc, g, REF, n, rs)
        rows.append([name, f(tc, 1), g, f(vm), f(pm, 1), f(voc)])
    print(md_table(["Condicao", "Tc (°C)", "G (W/m²)", "Vmp (V)", "Pmp (W)", "Voc (V)"], rows))

    print("\nMargens (Vmp - (Vbat + 1 V)) e (Voc - (Vbat + 5 V)):\n")
    rows = []
    for name, tc, g in scen[2:]:
        vm, im, pm, voc = mpp(tc, g, REF, n, rs)
        for vb, lab in ((13.3, "bulk 13,3 V"), (14.2, "absorcao 14,2 V")):
            rows.append([name, lab, f(vm - (vb + 1)), f(voc - (vb + 5))])
    print(md_table(["Condicao", "Vbat", "Vmp − (Vbat+1)", "Voc − (Vbat+5)"], rows))

    # Pior caso de partida com painel de Voc baixo e beta mais negativo (faixa tipica)
    print("\nPior caso de partida (faixa tipica, Voc_STC = 21,5 V, beta = -0,35 %/°C):\n")
    rows = []
    for ta in (20, ta_hot, 35):
        tc = tcell(ta, 1000, REF["noct"])
        voc = 21.5 * (1 - 0.0035 * (tc - 25))
        rows.append([f(ta, 0), f(tc, 1), f(voc), f(voc - 18.3), f(voc - 19.2)])
    print(md_table(["Ta (°C)", "Tc (°C)", "Voc (V)", "Voc − 18,3 V (bulk)", "Voc − 19,2 V (absorcao)"], rows))

    # Irradiancia minima para partida de manha (Tc=15 C) com Vbat 13,0 V
    a15 = n * REF["ns"] * vt(15)
    voc15 = REF["voc"] * (1 + REF["beta_voc"] * (15 - 25))
    g_min = 1000 * math.exp((13.0 + 5.0 - voc15) / a15)
    print(f"\nIrradiancia minima para Voc > Vbat+5 = 18 V com Tc = 15 °C (modelo): ~{g_min:.0f} W/m²\n")

    # ------------------------------------------------------------------ 4
    print("## 4. Ganho do MPPT sobre um PWM (painel preso em Vbat + 0,2 V)\n")
    rows = []
    for name, tc, g in [scen[0], scen[2], scen[3], scen[4]]:
        vm, im, pm, voc = mpp(tc, g, REF, n, rs)
        for vb in (13.3, 14.0):
            ipwm = iv_current(vb + 0.2, tc, g, REF, n, rs)
            p_pwm = vb * ipwm
            for eta in (0.95,):
                gain = eta * pm / p_pwm - 1
                rows.append([name, f(vb, 1), f(pm, 1), f(p_pwm, 1), f(eta * pm, 1), f(gain * 100, 0) + " %"])
    print(md_table(["Condicao", "Vbat (V)", "Pmp (W)", "P_PWM saida (W)", "P_MPPT saida η=0,95 (W)", "Ganho MPPT"], rows))

    # ------------------------------------------------------------------ 5
    print("\n## 5. Correntes FV e corrente de carga\n")
    vb = 13.3
    eta = CFG["mppt"]["efficiency"]
    rows = []
    for name, tc, g in [scen[0], scen[2], scen[3]]:
        vm, im, pm, voc = mpp(tc, g, REF, n, rs)
        pin = pm * CFG["pv"]["derate"]
        iout = eta * pin / vb
        rows.append([name, f(im), f(pin, 1), f(iout)])
    # sobre-irradiancia (borda de nuvem) 1200 W/m2, modulo ameno
    vm, im, pm, voc = mpp(tcell(20, 1200, REF["noct"]), 1200, REF, n, rs)
    pin = pm * CFG["pv"]["derate"]
    rows.append(["Borda de nuvem (1200 W/m², Ta 20 °C)", f(im), f(pin, 1), f(eta * pin / vb)])
    print(md_table(["Condicao", "Imp (A)", "P_FV com derate (W)", "I_saida MPPT a 13,3 V (A)"], rows))
    print(f"\nIsc x 1,25 = {REF['isc']*1.25:.2f} A (RNG-50P);  3,05 x 1,25 = {3.05*1.25:.2f} A (faixa tipica)")
    print(f"P_saida no limite de 3 A: 3 x 13,3 = {3*13.3:.1f} W; 3 x 14,2 = {3*14.2:.1f} W")

    # ------------------------------------------------------------------ 6
    print("\n## 6. Demanda diaria e noturna (perfil programado do system.yaml)\n")
    loads = {ld["key"]: ld for ld in CFG["loads"]}
    sched = CFG["demand"]["schedule"]
    aux_w = CFG["aux"]["p_w"]
    rows = []
    tot = {"dia": 0, "noite": 0, "total": 0}
    for key in ("led", "fan"):
        ld = loads[key]
        h = hours(sched[key])
        hn = overlap_h(sched[key], "18:00", "07:00")
        e = ld["p_nom_w"] * h
        en = ld["p_nom_w"] * hn
        rows.append([ld["name"], ld["priority"], f(ld["p_nom_w"], 1), f(h, 1), f(e, 1), f(e - en, 1), f(en, 1)])
        tot["total"] += e; tot["noite"] += en; tot["dia"] += e - en
    res = loads["res"]
    e_def = res["deferrable"]["daily_energy_wh"]
    t_def = e_def / res["p_nom_w"]
    rows.append([res["name"].split("(")[0].strip() + " (deslocavel, no dia)", res["priority"], f(res["p_nom_w"], 1),
                 f(t_def, 2), f(e_def, 1), f(e_def, 1), f(0, 1)])
    tot["total"] += e_def; tot["dia"] += e_def
    for p_aux in (0.7, aux_w):
        pass
    rows.append([f"Auxiliar (ESP32+buck+sensores) {f(aux_w,1)} W", "-", f(aux_w, 1), "24", f(aux_w * 24, 1),
                 f(aux_w * 11, 1), f(aux_w * 13, 1)])
    tot["total"] += aux_w * 24; tot["dia"] += aux_w * 11; tot["noite"] += aux_w * 13
    rows.append(["**Total**", "", "", "", f(tot["total"], 1), f(tot["dia"], 1), f(tot["noite"], 1)])
    print(md_table(["Carga", "Prioridade", "P nom. (W)", "h/dia", "E/dia (Wh)", "E dia 07–18 h (Wh)", "E noite 18–07 h (Wh)"], rows))
    # variante 0,7 W
    e07 = tot["total"] - aux_w * 24 + 0.7 * 24
    n07 = tot["noite"] - aux_w * 13 + 0.7 * 13
    print(f"\nCom P_aux = 0,7 W: total = {e07:.1f} Wh/dia; noite = {n07:.1f} Wh")
    conv_night = tot["noite"] + e_def
    print(f"Estrategia convencional (deslocavel 18-22 h): noite = {conv_night:.1f} Wh; dia = {tot['dia']-e_def:.1f} Wh")

    # Fator de tensao (P depende de V)
    for vv in (13.2, 13.6):
        fl = (vv / 12.8) ** 1.5
        fr = (vv / 12.8) ** 2
        e_v = (5 * 6.5 + 6 * 9) * fl + e_def * 1.0 + aux_w * 24  # deslocavel em energia fixa
        print(f"Fator de tensao a {vv} V: LED/ventilador x{fl:.3f}; resistor x{fr:.3f} (tempo menor p/ mesma energia). "
              f"Demanda nao deslocavel+aux = {e_v:.1f} Wh")

    e_nom = CFG["battery"]["nominal_v"] * CFG["battery"]["capacity_ah"]
    e_util = 0.8 * e_nom
    print(f"\nE_nom = {e_nom:.1f} Wh; util 20-100 % = {e_util:.1f} Wh; ate 10 % = {0.9*e_nom:.1f} Wh")
    print(f"Deficit noturno (EMS, aux {aux_w} W): {tot['noite']-e_util:.1f} Wh (eta=1); com eta_d=0,98: {tot['noite']/0.98-e_util:.1f} Wh")

    # ------------------------------------------------------------------ 7
    print("\n## 7. Geracao FV estimada x irradiacao diaria no plano (H_POA) — ESTIMATIVA\n")
    pr = 0.75
    rows = []
    e_need = tot["dia"] / 0.95 + (tot["noite"]) / (0.95 * 0.95)  # na entrada do MPPT, ida-volta 0,95 (estim.)
    for h in (1, 2, 3, 4, 5, 6):
        e_out = CFG["pv"]["p_stc_w"] * h * pr
        rows.append([h, f(e_out, 0), f(e_out / tot["total"] * 100, 0) + " %"])
    print(md_table(["H_POA (kWh/m²·dia)", "E_FV saida MPPT, PR=0,75 (Wh)", "Cobertura da demanda (144 Wh)"], rows))
    e_req_out = tot["dia"] + tot["noite"] / 0.95
    print(f"\nEnergia necessaria na SAIDA do MPPT (dia direto + noite via bateria, eta_ida-volta=0,95): {e_req_out:.0f} Wh"
          f" -> H_POA de equilibrio ~ {e_req_out/(CFG['pv']['p_stc_w']*pr):.1f} kWh/m2.dia")
    e_abs_max = tot["dia"] + e_util / 0.97
    print(f"Energia maxima aproveitavel num dia que comeca com SOC 20 % (cargas do dia + recarga 20->100 %): {e_abs_max:.0f} Wh")

    # ------------------------------------------------------------------ 8
    print("\n## 8. Autonomia e recarga\n")
    p_all = sum(l["p_nom_w"] for l in CFG["loads"]) + aux_w
    p_ess = loads["led"]["p_nom_w"] + aux_w
    rows = []
    for cap in (6.0, 12.0):
        en = 12.8 * cap
        u20 = 0.8 * en
        rows.append([f(cap, 0) + " Ah", f(en, 1), f(u20, 1), f(u20 / p_all, 1), f(u20 / p_ess, 1),
                     f(u20 / (tot["total"] / 24), 1), f(u20 / tot["total"], 2)])
    print(md_table(["Bateria", "E_nom (Wh)", "E util 20–100 % (Wh)", f"Todas as cargas+aux {f(p_all,1)} W (h)",
                    f"So LED+aux {f(p_ess,1)} W (h)", "Media diaria 6 W (h)", "Dias sem sol"], rows))
    rows = []
    for cap, ilim in ((6.0, 3.0), (12.0, 3.0), (12.0, 6.0)):
        ah = 0.8 * cap
        i_loads = (loads["fan"]["p_nom_w"] + aux_w) / 13.6
        i_pv_max = 0.95 * 50 * 0.93 / 13.6  # limite pela potencia do painel (STC, derate)
        i_eff = min(ilim, i_pv_max)
        rows.append([f(cap, 0) + " Ah", f(ilim, 1) + " A", f(i_eff, 2), f(ah / i_eff, 2), f(ah / (i_eff - i_loads), 2)])
    print(md_table(["Bateria", "Limite configurado", "I efetiva (A)", "20→100 % sem cargas (h)",
                    "20→100 % com ventilador+aux (h)"], rows))
    print("(corrente efetiva = min(limite, corrente que o painel de 50 W entrega a 13,6 V com derate); sem a fase de absorcao)")

    # ------------------------------------------------------------------ 9
    print("\n## 9. Correntes maximas por ramo\n")
    rows = []
    for ld in CFG["loads"]:
        for vv in (12.8, 14.6):
            p = ld["p_nom_w"] * (vv / ld["v_ref_v"]) ** ld["voltage_exponent"]
            rows.append([ld["key"], f(vv, 1), f(p, 2), f(p / vv, 3), f(ld["fuse_a"], 1)])
    print(md_table(["Carga", "V (V)", "P (W)", "I (A)", "Fusivel (A)"], rows))
    i_bus = sum(ld["p_nom_w"] * (14.6 / ld["v_ref_v"]) ** ld["voltage_exponent"] / 14.6 for ld in CFG["loads"])
    print(f"\nI_bus max (todas ligadas a 14,6 V) = {i_bus:.2f} A; a 12,8 V = "
          f"{sum(ld['p_nom_w']/12.8 for ld in CFG['loads']):.2f} A; a 11,6 V (LVD) = "
          f"{sum(ld['p_nom_w']*(11.6/12.8)**ld['voltage_exponent']/11.6 for ld in CFG['loads']):.2f} A")

    # ------------------------------------------------------------------ 10
    print("\n## 10. Perdas resistivas (estimativas)\n")
    rho = 0.0172  # Ohm.mm2/m, cobre a 20 C
    rows = []
    for name, i, r_ohm in [
        ("Shunt 10 mΩ a 3,0 A", 3.0, 0.010), ("Shunt 10 mΩ a 3,5 A", 3.5, 0.010),
        ("MOSFET IRLB8721, 16 mΩ máx. a VGS 4,5 V, 1,6 A", 1.6, 0.016),
        ("AO3400A, 48 mΩ máx. a VGS 2,5 V, 1,6 A", 1.6, 0.048),
    ]:
        rows.append([name, f(i * r_ohm * 1000, 0) + " mV", f(i * i * r_ohm * 1000, 0) + " mW"])
    for L, s, i in ((2, 2.5, 3.0), (10, 2.5, 2.8), (20, 2.5, 2.8), (20, 4.0, 2.8), (2, 1.0, 1.46)):
        r = rho * L / s
        rows.append([f"Cabo {f(s,1)} mm², laço {L} m, {f(i,2)} A", f(i * r * 1000, 0) + " mV", f(i * i * r * 1000, 0) + " mW"])
    print(md_table(["Elemento", "Queda", "Dissipação"], rows))

    # ------------------------------------------------------------------ 11
    print("\n## 11. Consumo auxiliar (estimativa por cenario)\n")
    rows = []
    for name, i3, ldo_iq, led, sens, tc_ma, eta_b, buck_iq_w in [
        ("Otimista (modem-sleep 40 mA, TC4427A, Pololu)", 0.040, 0.005, 0.001, 0.0013, 0.0004, 0.90, 0.0026),
        ("Nominal (Wi-Fi 68 mA, TC4427, Pololu)", 0.068, 0.005, 0.002, 0.0013, 0.0050, 0.85, 0.0026),
        ("Pessimista (RX contínuo 100 mA, TC4427, LM2596)", 0.100, 0.005, 0.002, 0.0028, 0.0090, 0.80, 0.13),
    ]:
        p5 = 5.0 * (i3 + ldo_iq + led + sens + tc_ma)
        pin = p5 / eta_b + buck_iq_w
        rows.append([name, f(p5, 2), f(pin, 2), f(pin * 24, 1), f(pin * 24 / e_nom * 100, 0) + " %"])
    print(md_table(["Cenário", "P em 5 V (W)", "P na entrada do buck (W)", "E/dia (Wh)", "% de E_nom/dia"], rows))
    print("MPPT Victron (autoconsumo ~10 mA, LOAD desligada): ~0,13 W -> ~1,7 Wh por noite de 13 h (fora do 'aux').")

    # ------------------------------------------------------------------ 12
    print("\n## 12. INA226: fundo de escala, resolucao e offset por shunt\n")
    rows = []
    for name, r in (("R100 (0,1 Ω)", 0.1), ("R010 nominal (10 mΩ)", 0.010), ("R010 efetivo medido (21 mΩ)", 0.021),
                    ("Externo 75 mV/10 A (7,5 mΩ)", 0.0075)):
        rows.append([name, f(0.08192 / r, 2), f(2.5e-6 / r * 1000, 3), f(10e-6 / r * 1000, 2),
                     f(10e-6 / r * 24 * 1000, 0), f(3.5 ** 2 * r, 3) if 3.5 <= 0.08192 / r else "satura"])
    print(md_table(["Shunt", "Fundo (A)", "Resolução (mA)", "Offset máx. (mA)", "Deriva SOC por offset (mAh/dia)",
                    "Dissipação a 3,5 A (W)"], rows))
    # orcamento de erro de SOC
    cap = CFG["battery"]["capacity_ah"]
    print(f"\nOffset 1 mA -> {24/cap/10:.2f} % de {cap} Ah por dia;"
          f" ganho 1 % -> {1/cap:.3f} p.p. de SOC por Ah integrado;"
          f" incerteza de capacidade ±8 % -> {0.08*100/cap:.2f} p.p. por Ah integrado")
    print(f"Fundo do INA226 'bat' com max_current_a=8 A: 8 A x 10 mOhm = {8*0.010*1000:.0f} mV (limite 81,92 mV);"
          f" com R_eff 10,5 mOhm -> {8*0.0105*1000:.0f} mV (satura)")

    # Pull-ups I2C
    print("\n## 13. Pull-ups I2C em paralelo\n")
    rows = []
    for name, rs_ in (("3x 10 kΩ (INA226) + 4,7 kΩ (DS3231)", [10e3] * 3 + [4.7e3]),
                      ("3x 4,7 kΩ + 4,7 kΩ", [4.7e3] * 4), ("3x 10 kΩ", [10e3] * 3)):
        req = 1.0 / sum(1.0 / x for x in rs_)
        rows.append([name, f(req / 1000, 2), f((3.3 - 0.4) / req * 1000, 2)])
    print(md_table(["Combinação (valores a CONFIRMAR nos módulos)", "R_eq (kΩ)", "I de sink em VOL=0,4 V (mA)"], rows))


if __name__ == "__main__":
    main()
