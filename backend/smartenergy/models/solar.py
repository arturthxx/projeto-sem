"""Geometria solar, céu claro, decomposição e transposição para o plano do módulo.

Modelos (docs/06 §6):

* **Posição solar** — algoritmo da NOAA (planilha "General Solar Position
  Calculations", baseada em Meeus): declinação, equação do tempo, ângulo
  horário, zênite e azimute. Precisão típica < 0,1° entre 1950 e 2050, mais
  que suficiente para um módulo de 50 W.
* **Irradiância extraterrestre** normal — série de Fourier de Spencer (1971).
* **Céu claro** — Haurwitz (1945): ``GHI = 1098·cosZ·exp(−0,057/cosZ)``.
* **Decomposição** global → difusa + direta — correlação de Erbs, Klein e
  Duffie (1982) em função do índice de claridade ``kt = GHI / (G0n·cosZ)``.
* **Transposição** — modelo isotrópico (Liu–Jordan) com componente refletida
  pelo solo (albedo padrão 0,2).

Convenções: ângulos em graus nas interfaces públicas; azimute medido a partir
do **Norte geográfico, sentido horário** (N = 0°, L = 90°, S = 180°, O = 270°).
No hemisfério Sul o módulo fica voltado para o Norte (``pv.azimuth_deg = 0``).
Todas as funções aceitam escalares ou ``numpy.ndarray`` (vetorizadas).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

SOLAR_CONSTANT_W_M2 = 1367.0
"""Constante solar usada por Spencer/Erbs (W/m²)."""

HAURWITZ_A = 1098.0
HAURWITZ_B = 0.057

MIN_COS_ZENITH = 0.01
"""Abaixo deste cosseno do zênite (elevação ≈ 0,57°) a irradiância é tratada como nula."""

DEFAULT_ALBEDO = 0.2

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class SolarPosition:
    """Posição solar (arrays alinhados ao vetor de tempo de entrada)."""

    zenith_deg: FloatArray
    azimuth_deg: FloatArray        # a partir do Norte, sentido horário
    declination_deg: FloatArray
    eot_min: FloatArray            # equação do tempo (min)
    hour_angle_deg: FloatArray     # negativo pela manhã, positivo à tarde

    @property
    def cos_zenith(self) -> FloatArray:
        return np.cos(np.radians(self.zenith_deg))

    @property
    def elevation_deg(self) -> FloatArray:
        return 90.0 - self.zenith_deg


def julian_day(ts_ms: ArrayLike) -> FloatArray:
    """Dia juliano a partir de epoch UTC em milissegundos."""
    return np.asarray(ts_ms, dtype=np.float64) / 86_400_000.0 + 2_440_587.5


def solar_position(ts_ms: ArrayLike, latitude_deg: float, longitude_deg: float) -> SolarPosition:
    """Posição solar pelo algoritmo da NOAA.

    Args:
        ts_ms: instante(s) em epoch UTC (ms).
        latitude_deg: latitude (positiva ao Norte; Cornélio Procópio ≈ −23,18°).
        longitude_deg: longitude (positiva a Leste; Cornélio Procópio ≈ −50,65°).

    Returns:
        :class:`SolarPosition` com zênite e azimute (N = 0°, sentido horário).
    """
    ts = np.atleast_1d(np.asarray(ts_ms, dtype=np.float64))
    jd = julian_day(ts)
    jc = (jd - 2_451_545.0) / 36_525.0  # século juliano

    l0 = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)  # longitude média (°)
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)                 # anomalia média (°)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m_r = np.radians(m)
    center = (
        np.sin(m_r) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + np.sin(2 * m_r) * (0.019993 - 0.000101 * jc)
        + np.sin(3 * m_r) * 0.000289
    )
    true_long = l0 + center
    omega = np.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * np.sin(omega)
    mean_obliq = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = mean_obliq + 0.00256 * np.cos(omega)
    obliq_r = np.radians(obliq)
    decl_r = np.arcsin(np.sin(obliq_r) * np.sin(np.radians(app_long)))

    y = np.tan(obliq_r / 2.0) ** 2
    l0_r = np.radians(l0)
    eot = 4.0 * np.degrees(
        y * np.sin(2 * l0_r)
        - 2 * ecc * np.sin(m_r)
        + 4 * ecc * y * np.sin(m_r) * np.cos(2 * l0_r)
        - 0.5 * y * y * np.sin(4 * l0_r)
        - 1.25 * ecc * ecc * np.sin(2 * m_r)
    )

    utc_min = np.mod(ts / 60_000.0, 1440.0)
    true_solar_min = np.mod(utc_min + eot + 4.0 * longitude_deg, 1440.0)
    hour_angle = true_solar_min / 4.0 - 180.0  # graus; 0 = meio-dia solar

    lat_r = np.radians(latitude_deg)
    ha_r = np.radians(hour_angle)
    cos_z = np.sin(lat_r) * np.sin(decl_r) + np.cos(lat_r) * np.cos(decl_r) * np.cos(ha_r)
    cos_z = np.clip(cos_z, -1.0, 1.0)
    zen_r = np.arccos(cos_z)

    sin_z = np.sin(zen_r)
    with np.errstate(divide="ignore", invalid="ignore"):
        cos_az_arg = (np.sin(lat_r) * cos_z - np.sin(decl_r)) / (np.cos(lat_r) * sin_z)
    cos_az_arg = np.clip(np.nan_to_num(cos_az_arg, nan=1.0), -1.0, 1.0)
    acos_deg = np.degrees(np.arccos(cos_az_arg))
    azimuth = np.where(hour_angle > 0.0, np.mod(acos_deg + 180.0, 360.0), np.mod(540.0 - acos_deg, 360.0))

    return SolarPosition(
        zenith_deg=np.degrees(zen_r),
        azimuth_deg=azimuth,
        declination_deg=np.degrees(decl_r),
        eot_min=eot,
        hour_angle_deg=hour_angle,
    )


def day_of_year(ts_ms: ArrayLike) -> FloatArray:
    """Dia do ano (1..366, fracionário) em UTC — suficiente para G0n."""
    jd = julian_day(ts_ms)
    # Dias desde 1º de janeiro de 2000 00:00 UTC, reduzidos ao ano trópico médio.
    days_since_2000 = jd - 2_451_544.5
    return np.mod(days_since_2000, 365.2422) + 1.0


def extraterrestrial_normal(ts_ms: ArrayLike) -> FloatArray:
    """Irradiância extraterrestre normal ``G0n`` (W/m²), série de Spencer (1971)."""
    gamma = 2.0 * np.pi * (day_of_year(ts_ms) - 1.0) / 365.0
    factor = (
        1.000110
        + 0.034221 * np.cos(gamma)
        + 0.001280 * np.sin(gamma)
        + 0.000719 * np.cos(2 * gamma)
        + 0.000077 * np.sin(2 * gamma)
    )
    return SOLAR_CONSTANT_W_M2 * factor


def clearsky_haurwitz(cos_zenith: ArrayLike) -> FloatArray:
    """GHI de céu claro de Haurwitz: ``1098·cosZ·exp(−0,057/cosZ)`` (W/m²); 0 com o Sol abaixo do limiar."""
    cz = np.asarray(cos_zenith, dtype=np.float64)
    out = np.zeros_like(cz)
    up = cz > MIN_COS_ZENITH
    out[up] = HAURWITZ_A * cz[up] * np.exp(-HAURWITZ_B / cz[up])
    return out


def erbs_diffuse_fraction(kt: ArrayLike) -> FloatArray:
    """Fração difusa ``kd = DHI/GHI`` pela correlação de Erbs et al. (1982)."""
    k = np.clip(np.asarray(kt, dtype=np.float64), 0.0, 1.0)
    mid = 0.9511 - 0.1604 * k + 4.388 * k**2 - 16.638 * k**3 + 12.336 * k**4
    return np.where(k <= 0.22, 1.0 - 0.09 * k, np.where(k <= 0.80, mid, 0.165))


@dataclass(frozen=True)
class Components:
    """Componentes da irradiância (W/m²)."""

    ghi: FloatArray
    dhi: FloatArray
    dni: FloatArray
    kt: FloatArray


def decompose_erbs(ghi: ArrayLike, cos_zenith: ArrayLike, g0n: ArrayLike) -> Components:
    """Separa GHI em difusa (DHI) e direta normal (DNI) usando Erbs.

    ``kt = GHI/(G0n·cosZ)``; ``DHI = kd(kt)·GHI``; ``DNI = (GHI − DHI)/cosZ``
    (limitada a ``G0n``). Com o Sol abaixo de ``MIN_COS_ZENITH`` tudo é zero.
    """
    g = np.maximum(np.asarray(ghi, dtype=np.float64), 0.0)
    cz = np.asarray(cos_zenith, dtype=np.float64)
    g0 = np.broadcast_to(np.asarray(g0n, dtype=np.float64), g.shape)
    up = cz > MIN_COS_ZENITH
    kt = np.zeros_like(g)
    kt[up] = g[up] / (g0[up] * cz[up])
    kt = np.clip(kt, 0.0, 1.0)
    dhi = erbs_diffuse_fraction(kt) * g
    dni = np.zeros_like(g)
    dni[up] = (g[up] - dhi[up]) / cz[up]
    dni = np.clip(dni, 0.0, g0)
    g_out = np.where(up, g, 0.0)
    dhi = np.where(up, dhi, 0.0)
    return Components(ghi=g_out, dhi=dhi, dni=dni, kt=kt)


def cos_incidence(
    zenith_deg: ArrayLike, azimuth_deg: ArrayLike, tilt_deg: float, surface_azimuth_deg: float
) -> FloatArray:
    """Cosseno do ângulo de incidência no plano (pode ser negativo: Sol atrás do módulo)."""
    z = np.radians(np.asarray(zenith_deg, dtype=np.float64))
    a = np.radians(np.asarray(azimuth_deg, dtype=np.float64))
    b = np.radians(tilt_deg)
    g = np.radians(surface_azimuth_deg)
    return np.cos(z) * np.cos(b) + np.sin(z) * np.sin(b) * np.cos(a - g)


def poa_isotropic(
    comps: Components,
    zenith_deg: ArrayLike,
    azimuth_deg: ArrayLike,
    tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float = DEFAULT_ALBEDO,
) -> FloatArray:
    """Irradiância no plano do módulo (W/m²) — modelo isotrópico com refletida.

    ``POA = DNI·max(0, cosθ) + DHI·(1 + cosβ)/2 + GHI·ρ·(1 − cosβ)/2``.
    """
    cos_t = cos_incidence(zenith_deg, azimuth_deg, tilt_deg, surface_azimuth_deg)
    b = np.radians(tilt_deg)
    beam = comps.dni * np.maximum(cos_t, 0.0)
    sky = comps.dhi * (1.0 + np.cos(b)) / 2.0
    ground = comps.ghi * albedo * (1.0 - np.cos(b)) / 2.0
    return np.maximum(beam + sky + ground, 0.0)


@dataclass(frozen=True)
class IrradianceResult:
    """Resultado da cadeia geometria → céu claro → plano do módulo."""

    position: SolarPosition
    g0n: FloatArray
    ghi_clear: FloatArray
    poa_clear: FloatArray


class SolarSite:
    """Local + orientação do módulo; encapsula a cadeia de irradiância.

    Args:
        latitude_deg, longitude_deg: coordenadas do local (``site``).
        tilt_deg, azimuth_deg: inclinação e azimute do módulo (``pv``; azimute 0 = Norte).
        albedo: refletância do solo (padrão 0,2).
    """

    def __init__(
        self,
        latitude_deg: float,
        longitude_deg: float,
        tilt_deg: float,
        azimuth_deg: float,
        albedo: float = DEFAULT_ALBEDO,
    ) -> None:
        self.latitude_deg = float(latitude_deg)
        self.longitude_deg = float(longitude_deg)
        self.tilt_deg = float(tilt_deg)
        self.azimuth_deg = float(azimuth_deg)
        self.albedo = float(albedo)

    @classmethod
    def from_config(cls, cfg) -> "SolarSite":
        """Cria a partir de ``SystemConfig`` (seções ``site`` e ``pv``)."""
        site, pv = cfg.raw["site"], cfg.raw["pv"]
        return cls(
            latitude_deg=float(site["latitude_deg"]),
            longitude_deg=float(site["longitude_deg"]),
            tilt_deg=float(pv["tilt_deg"]),
            azimuth_deg=float(pv["azimuth_deg"]),
            albedo=float(pv.get("albedo", DEFAULT_ALBEDO)),
        )

    def position(self, ts_ms: ArrayLike) -> SolarPosition:
        return solar_position(ts_ms, self.latitude_deg, self.longitude_deg)

    def clearsky(self, ts_ms: ArrayLike) -> IrradianceResult:
        """GHI e POA de céu claro (Haurwitz + Erbs + isotrópico) nos instantes dados."""
        pos = self.position(ts_ms)
        g0n = extraterrestrial_normal(np.atleast_1d(ts_ms))
        ghi_cs = clearsky_haurwitz(pos.cos_zenith)
        comps = decompose_erbs(ghi_cs, pos.cos_zenith, g0n)
        poa_cs = poa_isotropic(comps, pos.zenith_deg, pos.azimuth_deg, self.tilt_deg, self.azimuth_deg, self.albedo)
        return IrradianceResult(position=pos, g0n=g0n, ghi_clear=ghi_cs, poa_clear=poa_cs)

    def poa_from_ghi(self, ghi: ArrayLike, pos: SolarPosition, g0n: ArrayLike) -> FloatArray:
        """Transpõe uma GHI qualquer (ex.: com nuvens) para o plano do módulo."""
        comps = decompose_erbs(ghi, pos.cos_zenith, g0n)
        return poa_isotropic(comps, pos.zenith_deg, pos.azimuth_deg, self.tilt_deg, self.azimuth_deg, self.albedo)
