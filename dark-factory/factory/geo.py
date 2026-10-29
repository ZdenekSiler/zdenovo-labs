"""WGS84 GPS -> S-JTSK (the Czech national grid RÚIAN uses), stdlib only.

Helmert datum shift to the Bessel ellipsoid, then the Křovák conic projection. Returns
(Y, X) in metres, both positive, as RÚIAN's CSVs store them.

My first version was 7 km off: it applied the k constant to tan() when the formula is written
over tan²(). The output still looked like plausible S-JTSK coordinates, so nothing complained.
Now the reduce stage measures the conversion against listings that carry both GPS and an
exact RÚIAN address, and the run fails if the median error drifts past checks.GPS_MAX_MEDIAN_ERROR_M.
"""
from math import atan, cos, exp, log, pi, sin, sqrt

CZ_BOUNDS = (48.5, 51.1, 12.0, 18.9)  # lat_min, lat_max, lon_min, lon_max
GRID_M = 500


def in_czechia(lat: float, lon: float) -> bool:
    la0, la1, lo0, lo1 = CZ_BOUNDS
    return la0 <= lat <= la1 and lo0 <= lon <= lo1


def wgs84_to_sjtsk(lat: float, lon: float, h: float = 200.0) -> tuple[float, float]:
    # WGS84 geodetic -> geocentric
    b, l = lat * pi / 180, lon * pi / 180
    a, f1 = 6378137.0, 298.257223563
    e2 = 1 - (1 - 1 / f1) ** 2
    rho = a / sqrt(1 - e2 * sin(b) ** 2)
    x1, y1 = (rho + h) * cos(b) * cos(l), (rho + h) * cos(b) * sin(l)
    z1 = ((1 - e2) * rho + h) * sin(b)
    # Helmert: WGS84 -> S-JTSK datum
    dx, dy, dz = -570.69, -85.69, -462.84
    wx, wy, wz = (-s / 3600 * pi / 180 for s in (4.99821, 1.58676, 5.2611))
    m = 3.543e-6
    x2 = dx + (1 + m) * (x1 + wz * y1 - wy * z1)
    y2 = dy + (1 + m) * (-wz * x1 + y1 + wx * z1)
    z2 = dz + (1 + m) * (wy * x1 - wx * y1 + z1)
    # geocentric -> Bessel geodetic (Bowring)
    a, f1 = 6377397.15508, 299.152812853
    ab, e2 = f1 / (f1 - 1), 1 - (1 - 1 / f1) ** 2
    p = sqrt(x2 ** 2 + y2 ** 2)
    th = atan(z2 * ab / p)
    b = atan((z2 + e2 * ab * a * sin(th) ** 3) / (p - e2 * a * cos(th) ** 3))
    l = 2 * atan(y2 / (p + x2))
    # Křovák projection
    e, n, ro0 = 0.081696831215303, 0.97992470462083, 12310230.12797036
    sin_uq, cos_uq = 0.863499969506341, 0.504348889819882
    sin_vq, cos_vq = 0.420215144586493, 0.907424504992097
    alfa, k = 1.000597498371542, 1.003419163966575
    sb = sin(b)
    t = (1 + sb) ** 2 / (1 - sb ** 2) * exp(e * log((1 - e * sb) / (1 + e * sb)))
    t = k ** 2 * exp(alfa * log(t))  # tan² form, so k is squared too
    sin_u = (t - 1) / (t + 1)
    cos_u = sqrt(1 - sin_u ** 2)
    v = alfa * l
    cos_dv = cos_vq * cos(v) + sin_vq * sin(v)
    sin_dv = sin_vq * cos(v) - cos_vq * sin(v)
    sin_s = sin_uq * sin_u + cos_uq * cos_u * cos_dv
    cos_s = sqrt(1 - sin_s ** 2)
    sin_d = sin_dv * cos_u / cos_s
    eps = n * atan(sin_d / sqrt(1 - sin_d ** 2))
    ro = ro0 * exp(-n * log((1 + sin_s) / cos_s))
    return ro * sin(eps), ro * cos(eps)


def cell(y: float, x: float) -> str:
    return f"{int(y // GRID_M)},{int(x // GRID_M)}"


def neighbours(key: str):
    cy, cx = map(int, key.split(","))
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yield f"{cy + dy},{cx + dx}"
