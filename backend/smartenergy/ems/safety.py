"""Supervisor de segurança local (docs/06 §2) — espelho EXATO do firmware.

Executado a cada amostra (firmware: 100 ms; simulação: ``dt``). A saída física
de cada carga é ``on_final = on_EMS AND allowed``. Não substitui o BMS, os
fusíveis nem as proteções do controlador de carga.

Temporizadores ("por ≥ X s") são acumulados com ``dt`` enquanto a condição
persiste (a amostra em que a condição é observada conta o seu ``dt``), como no
estimador de SOC; zeram quando a condição deixa de valer. Uma medição ausente
(``None`` = sensor inválido) congela os temporizadores que dependem dela.

Prioridade do código reportado:
``OC_LOCKOUT > OC > OVP > OT > SENSOR > LVD > BOOT > OK``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smartenergy.config import SystemConfig

Event = tuple[str, str, str]  # (code, level, msg)

#: Histerese de liberação da sobretensão (docs/06 §2: ``bus_ovp_v − 0,3 V``).
OVP_RELEASE_HYST_V = 0.3
#: Janela "livre" que zera a contagem de disparos de sobrecorrente (docs/06 §2: 10 min).
OC_FREE_WINDOW_S = 600.0

CODE_PRIORITY = ("OC_LOCKOUT", "OC", "OVP", "OT", "SENSOR", "LVD", "BOOT")


@dataclass
class SafetyStatus:
    allowed: bool
    code: str                                        # OK | BOOT | LVD | OVP | OC | OC_LOCKOUT | OT | SENSOR
    events: list[Event] = field(default_factory=list)  # (code, level, msg) gerados nesta atualização
    active: tuple[str, ...] = ()                     # todas as proteções ativas (diagnóstico)


@dataclass(frozen=True)
class SafetyParams:
    lvd_v: float
    lvd_delay_s: float
    soc_hard_min: float
    reconnect_v: float
    reconnect_min_soc: float
    bus_ovp_v: float
    bus_ovp_delay_s: float
    bus_overcurrent_a: float
    bus_overcurrent_delay_s: float
    overcurrent_retry_s: float
    overcurrent_max_retries: int
    battery_overtemp_c: float
    battery_overtemp_release_c: float
    sensor_timeout_s: float
    boot_settle_samples: int

    @classmethod
    def from_config(cls, cfg: SystemConfig) -> "SafetyParams":
        s = cfg.raw["safety"]
        return cls(
            lvd_v=float(s["lvd_v"]),
            lvd_delay_s=float(s["lvd_delay_s"]),
            soc_hard_min=float(s["soc_hard_min"]),
            reconnect_v=float(s["reconnect_v"]),
            reconnect_min_soc=float(s["reconnect_min_soc"]),
            bus_ovp_v=float(s["bus_ovp_v"]),
            bus_ovp_delay_s=float(s["bus_ovp_delay_s"]),
            bus_overcurrent_a=float(s["bus_overcurrent_a"]),
            bus_overcurrent_delay_s=float(s["bus_overcurrent_delay_ms"]) / 1000.0,
            overcurrent_retry_s=float(s["overcurrent_retry_s"]),
            overcurrent_max_retries=int(s["overcurrent_max_retries"]),
            battery_overtemp_c=float(s["battery_overtemp_c"]),
            battery_overtemp_release_c=float(s["battery_overtemp_release_c"]),
            sensor_timeout_s=float(s["sensor_timeout_s"]),
            boot_settle_samples=int(s["boot_settle_samples"]),
        )


class SafetySupervisor:
    """Proteções BOOT/LVD/OVP/OC(+retry, lockout)/OT/SENSOR.

    ``boot_hold=True`` (firmware) mantém ``BOOT`` até ``boot_settle_samples``
    amostras válidas; a simulação e os vetores golden usam ``boot_hold=False``.
    """

    def __init__(self, cfg: SystemConfig, boot_hold: bool = False) -> None:
        self.p = SafetyParams.from_config(cfg)
        self.boot_hold = bool(boot_hold)
        self._pending: list[Event] = []
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Estado inicial (início de experimento / boot)."""
        self.boot_active = self.boot_hold
        self.boot_valid_samples = 0
        self.lvd_active = False
        self.t_lvd = 0.0
        self.ovp_active = False
        self.t_ovp = 0.0          # tempo acima do limiar (disparo)
        self.t_ovp_rel = 0.0      # tempo abaixo do limiar de liberação
        self.oc_active = False
        self.oc_lockout = False
        self.t_oc = 0.0           # tempo acima de bus_overcurrent_a
        self.t_oc_retry = 0.0     # tempo desde o disparo (nova tentativa)
        self.t_oc_free = 0.0      # tempo sem disparo, com a saída habilitada
        self.oc_trips = 0         # disparos sem 10 min livres
        self.ot_active = False
        self.sensor_active = False
        self.t_sensor = 0.0
        self._pending = []

    def reset_faults(self) -> None:
        """Rearme manual (comando ``reset_faults`` / botão): limpa OC e OC_LOCKOUT.

        LVD, OVP, OT e SENSOR se liberam sozinhos pelas condições de liberação.
        O evento ``FAULTS_RESET`` é emitido na próxima chamada de ``update``.
        """
        self.oc_active = False
        self.oc_lockout = False
        self.oc_trips = 0
        self.t_oc = 0.0
        self.t_oc_retry = 0.0
        self.t_oc_free = 0.0
        self._pending.append(("FAULTS_RESET", "info", "Falhas rearmadas manualmente"))

    @property
    def active(self) -> tuple[str, ...]:
        flags = {
            "OC_LOCKOUT": self.oc_lockout,
            "OC": self.oc_active,
            "OVP": self.ovp_active,
            "OT": self.ot_active,
            "SENSOR": self.sensor_active,
            "LVD": self.lvd_active,
            "BOOT": self.boot_active,
        }
        return tuple(c for c in CODE_PRIORITY if flags[c])

    # ------------------------------------------------------------------
    def update(
        self,
        t_s: float,
        dt_s: float,
        v_bat: float | None,
        soc: float,
        soc_valid: bool,
        v_bus: float | None = None,
        i_bus: float | None = None,
        t_bat: float | None = None,
    ) -> SafetyStatus:
        """Avalia todas as proteções para a amostra atual."""
        p = self.p
        dt = float(dt_s)
        events: list[Event] = self._pending
        self._pending = []

        v = v_bat if v_bat is not None else v_bus  # V_bat (substituto: bus.v)

        # --- SENSOR: bat e bus inválidos por mais de sensor_timeout_s -------
        if v_bat is None and v_bus is None:
            self.t_sensor += dt
            if not self.sensor_active and self.t_sensor > p.sensor_timeout_s:
                self.sensor_active = True
                events.append(("SENSOR_FAIL", "error", "Sensores de tensao bat e bus invalidos"))
        else:
            self.t_sensor = 0.0
            if self.sensor_active:
                self.sensor_active = False
                events.append(("SENSOR_OK", "info", "Sensor de tensao restabelecido"))

        # --- BOOT --------------------------------------------------------
        if self.boot_active:
            if v is not None:
                self.boot_valid_samples += 1
            if self.boot_valid_samples >= p.boot_settle_samples:
                self.boot_active = False

        # --- LVD (tensão com atraso ou SOC) -------------------------------
        if not self.lvd_active:
            if v is not None:
                self.t_lvd = self.t_lvd + dt if v < p.lvd_v else 0.0
            by_v = self.t_lvd >= p.lvd_delay_s
            by_soc = bool(soc_valid) and soc < p.soc_hard_min
            if by_v or by_soc:
                self.lvd_active = True
                self.t_lvd = 0.0
                if by_v:
                    events.append(("LVD_TRIP", "warn", f"Desconexao por subtensao: v_bat={v:.2f} V"))
                else:
                    events.append(("LVD_TRIP", "warn", f"Desconexao por SOC: soc={soc:.3f} < {p.soc_hard_min:.2f}"))
        else:
            if v is not None and v >= p.reconnect_v and ((not soc_valid) or soc >= p.reconnect_min_soc):
                self.lvd_active = False
                self.t_lvd = 0.0
                events.append(("LVD_CLEAR", "info", f"Religamento: v_bat={v:.2f} V"))

        # --- OVP no barramento ------------------------------------------
        if not self.ovp_active:
            self.t_ovp = self.t_ovp + dt if (v_bus is not None and v_bus > p.bus_ovp_v) else 0.0
            if self.t_ovp >= p.bus_ovp_delay_s:
                self.ovp_active = True
                self.t_ovp = 0.0
                self.t_ovp_rel = 0.0
                events.append(("OVP_TRIP", "error", f"Sobretensao no barramento: v_bus={v_bus:.2f} V"))
        else:
            release = v_bus is not None and v_bus < p.bus_ovp_v - OVP_RELEASE_HYST_V
            self.t_ovp_rel = self.t_ovp_rel + dt if release else 0.0
            if self.t_ovp_rel >= p.bus_ovp_delay_s:
                self.ovp_active = False
                self.t_ovp_rel = 0.0
                events.append(("OVP_CLEAR", "info", "Sobretensao no barramento eliminada"))

        # --- OC no barramento (nova tentativa / bloqueio) -----------------
        if not self.oc_lockout:
            if self.oc_active:
                self.t_oc_retry += dt
                if self.t_oc_retry >= p.overcurrent_retry_s:
                    self.oc_active = False
                    self.t_oc_retry = 0.0
                    self.t_oc = 0.0
                    events.append(("OC_RETRY", "info", f"Nova tentativa apos sobrecorrente ({self.oc_trips})"))
            else:
                self.t_oc = self.t_oc + dt if (i_bus is not None and i_bus > p.bus_overcurrent_a) else 0.0
                if self.t_oc >= p.bus_overcurrent_delay_s:
                    self.oc_trips += 1
                    self.t_oc = 0.0
                    self.t_oc_retry = 0.0
                    self.t_oc_free = 0.0
                    events.append(("OC_TRIP", "error", f"Sobrecorrente no barramento: i_bus={i_bus:.2f} A"))
                    if self.oc_trips >= p.overcurrent_max_retries:
                        self.oc_lockout = True
                        events.append(
                            ("OC_LOCKOUT", "critical", f"{self.oc_trips} disparos sem 10 min livres: bloqueio")
                        )
                    else:
                        self.oc_active = True
                else:
                    self.t_oc_free += dt
                    if self.oc_trips > 0 and self.t_oc_free >= OC_FREE_WINDOW_S:
                        self.oc_trips = 0

        # --- OT (temperatura da bateria) -----------------------------------
        if t_bat is not None:
            if not self.ot_active and t_bat > p.battery_overtemp_c:
                self.ot_active = True
                events.append(("OT_TRIP", "error", f"Sobretemperatura da bateria: {t_bat:.1f} C"))
            elif self.ot_active and t_bat < p.battery_overtemp_release_c:
                self.ot_active = False
                events.append(("OT_CLEAR", "info", f"Temperatura da bateria normalizada: {t_bat:.1f} C"))

        active = self.active
        code = active[0] if active else "OK"
        return SafetyStatus(allowed=not active, code=code, events=events, active=active)
