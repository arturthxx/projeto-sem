"""Gerenciamento preditivo — MPC com horizonte deslizante (docs/06 §3.3 e §3.4).

A cada ``replan_s`` (alinhado à grade de ``step_s``) resolve-se um MILP com
horizonte de ``horizon_steps`` passos; aplica-se apenas o passo corrente do
plano e os comandos são mantidos até o próximo replanejamento. Sobre o comando
do MPC valem as salvaguardas do §3.4 (nunca ligar sem pedido; ``soc <
critical_min_soc`` desliga medium/low). Se o solver falhar, não houver
previsão, a hora não estiver sincronizada ou o SOC for inválido, a decisão cai
para as regras (``fallback=True``, com ``status`` registrado).

Escolha de implementação (desempenho): o modelo é montado em PuLP e resolvido
pelo HiGHS via ``highspy`` (``pulp.HiGHS``; CBC não está disponível). Com o
núcleo do PuLP 4.x a montagem de 96 passos leva poucas dezenas de ms e o
solve tipicamente < 1 s (ver ``extra['build_ms']`` e ``plan['solve_ms']``), por
isso o modelo é reconstruído a cada replanejamento (código mais simples e
legível que atualizar coeficientes). O código é compatível com PuLP 2.x
(``LpVariable``/``LpStatus``) e 4.x (``add_variable``/``LpSolveStats``).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from smartenergy.config import LoadConfig, SystemConfig
from smartenergy.ems.base import Decision, EmsInputs, ForecastProvider, LoadCommand
from smartenergy.ems.rules import RuleBasedStrategy, RulesParams, deferrable_window_s
from smartenergy.timeutil import DAY_S, demand_at, in_window, tod_from_epoch_ms

#: Eficiência de descarga do modelo do MPC (docs/06 §3.3: η_d = 0,98).
ETA_DISCHARGE = 0.98

WEIGHT_KEYS = (
    "unserved_high",
    "unserved_medium",
    "unserved_low",
    "curtailment",
    "battery_throughput",
    "switching",
    "terminal_energy_value",
    "soc_min_violation",
)


# =============================================================================
# Problema de otimização (independente da estratégia — testável isoladamente)
# =============================================================================
@dataclass
class MpcLoad:
    """Carga no modelo do MPC."""

    key: str
    id: int
    priority: str
    p_w: float
    dimmable: bool = False
    min_duty: float = 0.0
    demand: list[float] | None = None          # fração pedida por passo (não deslocáveis)
    allowed: list[bool] | None = None          # passos dentro da janela considerada (deslocável)
    requirement_wh: float = 0.0                # energia exigida na janela considerada (deslocável)
    initial_on: bool = False                   # estado atual (u_j[−1])

    @property
    def deferrable(self) -> bool:
        return self.allowed is not None


@dataclass
class MpcProblem:
    step_s: float
    e_nom_wh: float
    e0_wh: float
    soc_min: float
    soc_max: float
    eta_c: float
    eta_d: float
    pc_max_w: float
    pd_max_w: float
    p_aux_w: float
    pv_avail_w: list[float]                    # P̂pv na saída do MPPT (W)
    loads: list[MpcLoad]
    weights: dict[str, float]

    @property
    def n(self) -> int:
        return len(self.pv_avail_w)


@dataclass
class MpcSolution:
    ok: bool
    status: str
    objective: float | None = None
    solve_ms: float = 0.0
    build_ms: float = 0.0
    soc: list[float] = field(default_factory=list)          # N+1 valores (e[k]/E_nom)
    u: dict[str, list[float]] = field(default_factory=dict)  # duty por passo por chave
    pc_w: list[float] = field(default_factory=list)
    pd_w: list[float] = field(default_factory=list)
    curtail_w: list[float] = field(default_factory=list)
    deficit_wh: dict[str, float] = field(default_factory=dict)


def _add_var(prob: Any, name: str, lo: float | None, hi: float | None, cat: str = "Continuous") -> Any:
    """Cria variável em PuLP 4.x (``add_variable``) ou 2.x (``LpVariable``)."""
    import pulp

    if hasattr(prob, "add_variable"):
        return prob.add_variable(name, lo, hi, cat)
    return pulp.LpVariable(name, lowBound=lo, upBound=hi, cat=cat)  # pragma: no cover - PuLP 2.x


def make_solver(time_limit_s: float, mip_gap: float) -> Any:
    """Solver HiGHS do PuLP (API ``highspy``; alternativa ``HiGHS_CMD``)."""
    import pulp

    for cls_name in ("HiGHS", "HiGHS_CMD"):
        cls = getattr(pulp, cls_name, None)
        if cls is None:
            continue
        solver = cls(msg=False, timeLimit=time_limit_s, gapRel=mip_gap)
        if solver.available():
            return solver
    raise RuntimeError("solver HiGHS indisponível no PuLP (instale highspy)")


def _solve(prob: Any, solver: Any) -> tuple[str, bool]:
    """Resolve e retorna ``(status, tem_solução)`` para PuLP 2.x e 4.x."""
    import pulp

    res = prob.solve(solver)
    if hasattr(res, "status_str"):  # PuLP 4.x: LpSolveStats
        return str(res.status_str), bool(res.has_solution)
    status = pulp.LpStatus[prob.status]  # pragma: no cover - PuLP 2.x
    return status, prob.sol_status in (1, 2)  # pragma: no cover


def solve_mpc(problem: MpcProblem, time_limit_s: float = 10.0, mip_gap: float = 0.01) -> MpcSolution:
    """Monta e resolve o MILP do docs/06 §3.3."""
    import pulp

    t_build = time.perf_counter()
    P = problem
    n = P.n
    dt_h = P.step_s / 3600.0
    w = P.weights
    alpha = {"high": w["unserved_high"], "medium": w["unserved_medium"], "low": w["unserved_low"]}
    prob = pulp.LpProblem("sem_mpc", pulp.LpMinimize)

    pc = [_add_var(prob, f"pc_{k}", 0.0, P.pc_max_w) for k in range(n)]
    pd = [_add_var(prob, f"pd_{k}", 0.0, P.pd_max_w) for k in range(n)]
    pcu = [_add_var(prob, f"pcu_{k}", 0.0, max(0.0, P.pv_avail_w[k])) for k in range(n)]
    e: list[Any] = [P.e0_wh] + [_add_var(prob, f"e_{k}", None, P.soc_max * P.e_nom_wh) for k in range(1, n + 1)]
    v = [_add_var(prob, f"v_{k}", 0.0, None) for k in range(1, n + 1)]

    obj_terms: list[Any] = []
    const_obj = 0.0
    u: dict[str, list[Any]] = {}
    z: dict[str, list[Any]] = {}
    sh: dict[str, Any] = {}
    for ld in P.loads:
        zs: list[Any] = []
        us: list[Any] = []
        for k in range(n):
            if ld.deferrable:
                ub = 1.0 if ld.allowed[k] else 0.0  # type: ignore[index]
                zk = _add_var(prob, f"z_{ld.key}_{k}", 0.0, ub, "Binary")
                uk = zk
            else:
                d = float(ld.demand[k]) if ld.demand is not None else 0.0
                d = min(1.0, max(0.0, d))
                zk = _add_var(prob, f"z_{ld.key}_{k}", 0.0, float(math.ceil(d)), "Binary")
                if ld.dimmable:
                    uk = _add_var(prob, f"u_{ld.key}_{k}", 0.0, d)
                    prob += uk >= ld.min_duty * zk
                    prob += uk <= zk
                else:
                    uk = zk
                # α_p·P·(d − u)·Δ  (parte constante somada à parte)
                a = alpha.get(ld.priority, alpha["low"])
                const_obj += a * ld.p_w * d * dt_h
                obj_terms.append(-a * ld.p_w * dt_h * uk)
            zs.append(zk)
            us.append(uk)
        z[ld.key] = zs
        u[ld.key] = us
        # comutações (sobre a variável binária on/off)
        prev: Any = 1.0 if ld.initial_on else 0.0
        for k in range(n):
            s = _add_var(prob, f"s_{ld.key}_{k}", 0.0, None)
            prob += s >= zs[k] - prev
            prob += s >= prev - zs[k]
            obj_terms.append(w["switching"] * s)
            prev = zs[k]
        if ld.deferrable:
            shv = _add_var(prob, f"sh_{ld.key}", 0.0, None)
            delivered = pulp.lpSum(ld.p_w * dt_h * us[k] for k in range(n) if ld.allowed[k])  # type: ignore[index]
            prob += shv + delivered >= ld.requirement_wh
            # Limite superior (complemento ao docs/06, ver contract_change_requests): a
            # deslocável não opera além do requisito arredondado para passos inteiros,
            # como impõe a salvaguarda (desliga com restante == 0). Evita usá-la como
            # "carga de despejo" do excedente e reduz a simetria do MILP.
            e_step = ld.p_w * dt_h
            if e_step > 0:
                cap = math.ceil(ld.requirement_wh / e_step - 1e-9) * e_step
                prob += delivered <= cap
            sh[ld.key] = shv
            obj_terms.append(alpha["low"] * shv)

    for k in range(n):
        load_k = pulp.lpSum(ld.p_w * u[ld.key][k] for ld in P.loads)
        # balanço: P̂pv − pcu + pd = pc + Σ P·u + P_aux
        prob += P.pv_avail_w[k] - pcu[k] + pd[k] - pc[k] - load_k == P.p_aux_w
        prob += e[k + 1] == e[k] + (P.eta_c * dt_h) * pc[k] - (dt_h / P.eta_d) * pd[k]
        prob += e[k + 1] + v[k] >= P.soc_min * P.e_nom_wh
        obj_terms.append(w["curtailment"] * dt_h * pcu[k])
        obj_terms.append(w["battery_throughput"] * dt_h * (pc[k] + pd[k]))
        obj_terms.append(w["soc_min_violation"] * v[k])
    obj_terms.append(-w["terminal_energy_value"] * e[n])
    prob += pulp.lpSum(obj_terms) + const_obj
    build_ms = (time.perf_counter() - t_build) * 1000.0

    t_solve = time.perf_counter()
    try:
        status, has_sol = _solve(prob, make_solver(time_limit_s, mip_gap))
    except Exception as exc:  # solver ausente ou erro interno
        return MpcSolution(ok=False, status=f"error: {exc}", build_ms=build_ms,
                           solve_ms=(time.perf_counter() - t_solve) * 1000.0)
    solve_ms = (time.perf_counter() - t_solve) * 1000.0
    if not has_sol:
        return MpcSolution(ok=False, status=status, build_ms=build_ms, solve_ms=solve_ms)

    def val(x: Any) -> float:
        return float(x) if isinstance(x, (int, float)) else float(x.value() or 0.0)

    sol = MpcSolution(
        ok=True,
        status=status,
        objective=float(pulp.value(prob.objective)),
        build_ms=build_ms,
        solve_ms=solve_ms,
        soc=[val(e[k]) / P.e_nom_wh for k in range(n + 1)],
        pc_w=[val(x) for x in pc],
        pd_w=[val(x) for x in pd],
        curtail_w=[val(x) for x in pcu],
        deficit_wh={k: val(x) for k, x in sh.items()},
    )
    for ld in P.loads:
        if ld.dimmable and not ld.deferrable:
            sol.u[ld.key] = [val(u[ld.key][k]) if val(z[ld.key][k]) > 0.5 else 0.0 for k in range(n)]
        else:
            sol.u[ld.key] = [1.0 if val(z[ld.key][k]) > 0.5 else 0.0 for k in range(n)]
    return sol


# =============================================================================
# Salvaguardas (§3.4)
# =============================================================================
def apply_safeguards(
    cfg: SystemConfig, rp: RulesParams, cmds: Mapping[str, LoadCommand], inp: EmsInputs
) -> tuple[dict[str, LoadCommand], list[str]]:
    """Aplica as salvaguardas do modo preditivo sobre comandos do MPC."""
    out: dict[str, LoadCommand] = {}
    notes: list[str] = []
    low_soc = inp.soc < rp.critical_min_soc
    for ld in cfg.loads:
        c = cmds.get(ld.key)
        on = bool(c.on) if c is not None else False
        duty = float(c.duty) if c is not None else 0.0
        if ld.deferrable is not None:
            remaining = ld.deferrable.daily_energy_wh - float(inp.deferrable_done_wh.get(ld.key, 0.0))
            ws, dl = deferrable_window_s(ld)
            if on and (remaining <= 0.0 or not in_window(inp.tod_s, ws, dl)):
                on = False
                notes.append(f"{ld.key}: sem pedido (janela/energia)")
        elif on and not inp.demand.get(ld.key, False):
            on = False
            notes.append(f"{ld.key}: sem demanda")
        if on and low_soc and ld.priority in ("medium", "low"):
            on = False
            notes.append(f"{ld.key}: soc {inp.soc:.3f} < {rp.critical_min_soc:.2f}")
        if not on:
            duty = 0.0
        elif ld.dimmable:
            duty = min(1.0, max(ld.min_duty, duty))
        else:
            duty = 1.0
        out[ld.key] = LoadCommand(id=ld.id, key=ld.key, on=on, duty=duty)
    return out, notes


# =============================================================================
# Estratégia
# =============================================================================
class PredictiveStrategy:
    """MPC com horizonte deslizante + salvaguardas + *fallback* para regras."""

    name = "predictive"

    def __init__(
        self,
        cfg: SystemConfig,
        forecast: ForecastProvider | None = None,
        forecast_method: str | None = None,
        weights: Mapping[str, float] | None = None,
        time_limit_s: float | None = None,
        mip_gap: float | None = None,
        **_: Any,
    ) -> None:
        self.cfg = cfg
        mp = cfg.raw["ems"]["predictive"]
        self.step_s = int(mp["step_s"])
        self.n = int(mp["horizon_steps"])
        self.replan_s = int(mp["replan_s"])
        self.ttl_s = float(mp["command_ttl_s"])
        # time_limit_s/mip_gap podem ser reduzidos em varreduras longas de simulação
        self.time_limit_s = float(time_limit_s if time_limit_s is not None else mp["time_limit_s"])
        self.mip_gap = float(mip_gap if mip_gap is not None else mp["mip_gap"])
        self.soc_min = float(mp["soc_min"])
        self.soc_max = float(mp["soc_max"])
        self.weights = {k: float(mp["weights"][k]) for k in WEIGHT_KEYS}
        if weights:
            self.weights.update({k: float(v) for k, v in weights.items()})
        b = cfg.raw["battery"]
        self.v_nom = float(b["nominal_v"])
        self.e_nom = cfg.battery_energy_wh
        self.eta_c = float(b["coulombic_efficiency"])
        self.pc_max = self.v_nom * float(cfg.raw["mppt"]["max_charge_current_a"])
        self.pd_max = self.v_nom * float(b["max_discharge_current_a"])
        self.mppt_eff = float(cfg.raw["mppt"]["efficiency"])
        self.p_aux = float(cfg.raw["aux"]["p_w"])
        self.rules_params = RulesParams.from_config(cfg)
        self.forecast_method = forecast_method or str(cfg.raw["forecast"]["pv_method"])
        self._own_forecast: ForecastProvider | None = forecast
        self._own_forecast_error: str | None = None
        self._rules = RuleBasedStrategy(cfg)
        self.reset()

    def reset(self) -> None:
        self._rules.reset()
        self.last_plan: dict[str, Any] | None = None   # último plano válido (docs/05 §5)
        self._plan_u: dict[str, list[float]] | None = None
        self._plan_t0_ms: int | None = None
        self._plan_made_ms: int | None = None
        self._plan_slot: int | None = None
        self.last_status: str | None = None
        self.n_solves = 0
        self.n_fallbacks = 0

    # ------------------------------------------------------------------
    def _default_forecast(self) -> ForecastProvider | None:
        """Constrói (uma vez) o provedor padrão de previsão (import tardio)."""
        if self._own_forecast is None and self._own_forecast_error is None:
            try:
                from smartenergy.forecast.factory import make_forecast  # módulo de outro pacote
            except ImportError as exc:
                self._own_forecast_error = f"previsao indisponivel (smartenergy.forecast.factory): {exc}"
                return None
            try:
                self._own_forecast = make_forecast(self.forecast_method, self.cfg)
            except Exception as exc:  # método desconhecido / configuração inválida
                self._own_forecast_error = f"previsao indisponivel ({self.forecast_method}): {exc}"
                return None
        return self._own_forecast

    def _fallback(self, rules_dec: Decision, why: str, status: str) -> Decision:
        self.n_fallbacks += 1
        self.last_status = status
        return Decision(
            mode=self.name,
            state=rules_dec.state,
            commands=rules_dec.commands,
            reason=f"fallback regras ({why}): {rules_dec.reason}",
            fallback=True,
            plan=None,
            extra={"status": status, "fallback_reason": why, "rules": rules_dec.extra},
        )

    def _demand_fractions(
        self, fc: ForecastProvider, t0_ms: int, tod0: float, keys: Sequence[str]
    ) -> dict[str, list[float]]:
        try:
            dem = dict(fc.demand_forecast(t0_ms, self.step_s, self.n) or {})
        except Exception:
            dem = {}
        out: dict[str, list[float]] = {}
        for key in keys:
            if key in dem and len(dem[key]) >= self.n:
                out[key] = [float(x) for x in dem[key][: self.n]]
            else:  # perfil programado, amostrado por minuto
                sub = max(1, self.step_s // 60)
                out[key] = [
                    sum(demand_at(self.cfg, key, tod0 + k * self.step_s + (j + 0.5) * self.step_s / sub)
                        for j in range(sub)) / sub
                    for k in range(self.n)
                ]
        return out

    def _deferrable_requirement(
        self, ld: LoadConfig, tod_now: float, tod0: float, done_wh: float
    ) -> tuple[list[bool], float]:
        """Passos permitidos e energia exigida da deslocável no horizonte.

        Considera a ocorrência da janela que ainda não terminou: a de hoje
        (requisito = diário − já entregue hoje) ou, se a de hoje já terminou, a de
        amanhã (requisito completo) — sempre restrita aos passos do horizonte.
        """
        assert ld.deferrable is not None
        ws, dl = deferrable_window_s(ld)
        span = (dl - ws) % DAY_S
        occ: tuple[float, float, int] | None = None
        for day in (-1, 0, 1, 2):
            a = day * DAY_S + ws
            b = a + span
            if b > tod_now:
                occ = (a, b, day)
                break
        assert occ is not None
        a, b, day = occ
        daily = ld.deferrable.daily_energy_wh
        today = day <= 0  # ocorrência de hoje (ou iniciada ontem e ainda aberta)
        req = max(0.0, daily - done_wh) if today else daily
        allowed = [a <= tod0 + k * self.step_s < b for k in range(self.n)]
        return allowed, req

    def build_problem(self, inp: EmsInputs, fc: ForecastProvider, t0_ms: int) -> tuple[MpcProblem, list[float]]:
        """Monta o problema do MPC para o instante ``t0_ms`` (início da grade)."""
        tod0 = tod_from_epoch_ms(t0_ms, self.cfg)
        tod_now = tod0 + (inp.ts_ms - t0_ms) / 1000.0
        pv_raw = [max(0.0, float(x)) for x in fc.pv_forecast(t0_ms, self.step_s, self.n)][: self.n]
        if len(pv_raw) < self.n:
            raise ValueError(f"previsao FV com {len(pv_raw)} passos (< {self.n})")
        non_def = [ld.key for ld in self.cfg.loads if ld.deferrable is None]
        dem = self._demand_fractions(fc, t0_ms, tod0, non_def)
        loads: list[MpcLoad] = []
        for ld in self.cfg.loads:
            st = inp.outputs.get(ld.key)
            m = MpcLoad(
                key=ld.key, id=ld.id, priority=ld.priority, p_w=ld.p_nom_w,
                dimmable=ld.dimmable and ld.deferrable is None, min_duty=ld.min_duty,
                initial_on=bool(st.on) if st is not None else False,
            )
            if ld.deferrable is not None:
                done = float(inp.deferrable_done_wh.get(ld.key, 0.0))
                m.allowed, m.requirement_wh = self._deferrable_requirement(ld, tod_now, tod0, done)
            else:
                m.demand = dem[ld.key]
            loads.append(m)
        problem = MpcProblem(
            step_s=float(self.step_s),
            e_nom_wh=self.e_nom,
            e0_wh=min(1.0, max(0.0, inp.soc)) * self.e_nom,
            soc_min=self.soc_min,
            soc_max=self.soc_max,
            eta_c=self.eta_c,
            eta_d=ETA_DISCHARGE,
            pc_max_w=self.pc_max,
            pd_max_w=self.pd_max,
            p_aux_w=self.p_aux,
            pv_avail_w=[p * self.mppt_eff for p in pv_raw],
            loads=loads,
            weights=dict(self.weights),
        )
        return problem, pv_raw

    def _replan(self, inp: EmsInputs, fc: ForecastProvider) -> MpcSolution:
        step_ms = self.step_s * 1000
        t0_ms = (inp.ts_ms // step_ms) * step_ms
        problem, pv_raw = self.build_problem(inp, fc, t0_ms)
        sol = solve_mpc(problem, self.time_limit_s, self.mip_gap)
        self.n_solves += 1
        self.last_status = sol.status
        if sol.ok:
            self._plan_u = sol.u
            self._plan_t0_ms = t0_ms
            self._plan_made_ms = inp.ts_ms
            self.last_plan = {
                "step_s": self.step_s,
                "t0_ms": t0_ms,
                "soc": [round(x, 5) for x in sol.soc],
                "loads": {str(ld.id): [round(x, 4) for x in sol.u[ld.key]] for ld in self.cfg.loads},
                "pv_fc_w": [round(x, 3) for x in pv_raw],
                "objective": round(sol.objective, 6) if sol.objective is not None else None,
                "solve_ms": round(sol.solve_ms, 2),
                "status": sol.status,
                "build_ms": round(sol.build_ms, 2),
                "p_bat_w": [round(d - c, 3) for c, d in zip(sol.pc_w, sol.pd_w)],
                "curtail_w": [round(x, 3) for x in sol.curtail_w],
                "deficit_wh": {k: round(x, 4) for k, x in sol.deficit_wh.items()},
            }
        else:
            self._plan_u = None
        return sol

    # ------------------------------------------------------------------
    def decide(self, inp: EmsInputs, forecast: ForecastProvider | None = None) -> Decision:
        rules_dec = self._rules.decide(inp)  # mantém a histerese em dia (fallback e estado informativo)
        if inp.ts_ms <= 0:
            return self._fallback(rules_dec, "hora nao sincronizada", "no_time")
        if not inp.soc_valid:
            return self._fallback(rules_dec, "soc invalido", "soc_invalid")
        fc = forecast
        if fc is None:
            fc = self._default_forecast()
            if fc is None:
                return self._fallback(rules_dec, self._own_forecast_error or "sem previsao", "no_forecast")
            try:  # o provedor próprio recebe as medições; um provedor externo é alimentado pelo chamador
                fc.observe(inp.ts_ms, inp.p_pv)
            except Exception:
                pass

        slot = inp.ts_ms // (self.replan_s * 1000)
        new_plan = False
        if slot != self._plan_slot:
            self._plan_slot = slot
            try:
                sol = self._replan(inp, fc)
            except Exception as exc:
                self._plan_u = None
                self.last_status = f"error: {exc}"
                return self._fallback(rules_dec, f"erro ao montar o MPC: {exc}", self.last_status)
            new_plan = sol.ok
        if self._plan_u is None or self._plan_t0_ms is None:
            return self._fallback(rules_dec, f"solver sem solucao ({self.last_status})", str(self.last_status))

        k = int((inp.ts_ms - self._plan_t0_ms) // (self.step_s * 1000))
        age_s = (inp.ts_ms - (self._plan_made_ms or inp.ts_ms)) / 1000.0
        if k < 0 or k >= self.n or age_s > self.ttl_s:
            self._plan_u = None
            return self._fallback(rules_dec, "plano expirado", "expired")

        raw: dict[str, LoadCommand] = {}
        for ld in self.cfg.loads:
            x = self._plan_u[ld.key][k]
            raw[ld.key] = LoadCommand(id=ld.id, key=ld.key, on=x > 1e-6, duty=x)
        cmds, notes = apply_safeguards(self.cfg, self.rules_params, raw, inp)
        reason = f"mpc: passo {k} do plano ({self.last_plan['status'] if self.last_plan else '?'})"
        if notes:
            reason += "; salvaguardas: " + ", ".join(notes)
        return Decision(
            mode=self.name,
            state=rules_dec.state,
            commands=cmds,
            reason=reason,
            fallback=False,
            plan=self.last_plan if new_plan else None,
            extra={"plan_step": k, "plan_age_s": age_s, "status": self.last_status, "new_plan": new_plan},
        )
