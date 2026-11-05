# Interfaces de estimação e segurança (contrato entre simulação e algoritmos)

Implementações em `estimation/soc.py` e `ems/safety.py` (espelhos exatos do firmware,
ver `docs/06-especificacao-algoritmos.md` §1 e §2).

```python
class SocEstimator:
    def __init__(self, cfg: SystemConfig, initial_soc: float | None = None,
                 initial_unc: float | None = None, v_init: float | None = None): ...
        # initial_soc dado -> last_sync="persisted"; senão OCV^-1(v_init) -> "ocv_init"
    def update(self, dt_s: float, i_a: float, v_v: float, sensor_ok: bool = True) -> list[tuple[str, str, str]]:
        # retorna eventos [(code, level, msg)], ex.: ("SOC_SYNC_FULL","info","...") apenas na borda
    def sync(self, soc: float) -> None            # comando sync_soc (last_sync="manual")
    soc: float; unc: float; valid: bool; last_sync: str; since_sync_s: float; ah_since_sync: float

@dataclass
class SafetyStatus:
    allowed: bool
    code: str                                     # OK | BOOT | LVD | OVP | OC | OC_LOCKOUT | OT | SENSOR
    events: list[tuple[str, str, str]]            # (code, level, msg) gerados nesta atualização

class SafetySupervisor:
    def __init__(self, cfg: SystemConfig, boot_hold: bool = False): ...  # simulação usa boot_hold=False
    def reset(self) -> None
    def update(self, t_s: float, dt_s: float, v_bat: float | None, soc: float, soc_valid: bool,
               v_bus: float | None = None, i_bus: float | None = None, t_bat: float | None = None) -> SafetyStatus
    def reset_faults(self) -> None
```
