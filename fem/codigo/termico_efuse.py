"""Simulacion termica (COMSOL, Heat Transfer in Solids) del eFuse LTC4215 a 12 V, 15 A.

Mismo metodo validado en el proyecto LM2596 (BUCK_LM259/fem):
  - Geometria: STEP de KiCad SOLO con placa y componentes (sin cobre como solidos:
    mallar laminas de 35 um no cabe en memoria). Importado y unido (Form Union).
  - Cobre de F.Cu y B.Cu como capa delgada de 35 um con la forma real, a partir de
    mascaras exportadas de KiCad (geometria/mascara_cobre_*.txt).
  - Soldadura (SAC305) en el hueco de 85 um entre placa y componentes (los modelos 3D
    de KiCad empiezan en z = 1.595 mm; la placa termina en 1.51 mm).
  - Vias como cilindros de cobre equivalente, leidas del .kicad_pcb (posiciones y taladro).
  - Calentamiento I^2R del propio cobre: Electric Currents, Shell en las mismas capas,
    acoplado con la temperatura (la resistividad del cobre sube 0.393 %/K).
  - Fuentes como "Heat rate": R5 en su volumen, Q1 en la cara entre su pad de drenaje
    y el encapsulado (donde esta el die), U1 en la cara de su pad expuesto.
  - Conveccion natural con correlaciones explicitas segun la orientacion de la cara,
    25 degC, sin radiacion (como LM317/LM2596). Radiacion como variante.
Coordenadas: las del STEP de KiCad (mm): x 17.5..91.5, y -79.5..-60 (y = -y_KiCad).

Uso (con limite de memoria):
  systemd-run --user --scope -p MemoryMax=10G python fem/codigo/termico_efuse.py [con_vias|sin_vias]
  con_vias (por defecto): placa actual de kicad/EFUSE (con vias termicas bajo R5 y Q1)
  sin_vias: placa anterior (geometria/sin_vias), para comparar
Salidas: fem/EFUSE_termico.mph, fem/resultados/*.csv, fem/figuras/*.png
         (sin_vias: en resultados/sin_vias_con_joule y figuras/sin_vias_con_joule)
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import mph
import numpy as np

FEM = Path(__file__).resolve().parent.parent
VARIANTE = sys.argv[1] if len(sys.argv) > 1 else 'con_vias'
if VARIANTE == 'con_vias':
    GEO = FEM / 'geometria'
    PCB = FEM.parent / 'kicad' / 'EFUSE' / 'EFUSE.kicad_pcb'
    RES = FEM / 'resultados'
    FIG = FEM / 'figuras'
    MODELO = FEM / 'EFUSE_termico.mph'
elif VARIANTE == 'sin_vias':
    GEO = FEM / 'geometria' / 'sin_vias'
    PCB = GEO / 'EFUSE_sin_vias.kicad_pcb'
    RES = FEM / 'resultados' / 'sin_vias_con_joule'
    FIG = FEM / 'figuras' / 'sin_vias_con_joule'
    MODELO = RES / 'EFUSE_termico_sin_vias.mph'
else:
    raise SystemExit(f'variante desconocida: {VARIANTE}')

Z_PCB, Z_COMP = 1.51, 1.595
ESCALA_Z = 10
# Mascaras: X = x_KiCad - 17.5, Y = 79.5 - y_KiCad; en el STEP y = -y_KiCad -> X = x - 17.5, Y = y + 79.5
X0_MASC, Y0_MASC = 17.5, 79.5



def vias_kicad(pcb: Path) -> list[tuple[float, float, float, str]]:
    """(x, y, radio del taladro, red) de cada via del .kicad_pcb (coordenadas KiCad, mm)."""
    texto = pcb.read_text()
    redes = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', texto))
    vias = []
    for m in re.finditer(r'\(via\b.*?\(at ([\d.\-]+) ([\d.\-]+)\).*?\(drill ([\d.]+)\)(.*?)\(uuid', texto, re.S):
        red = re.search(r'\(net (?:(\d+)|"([^"]*)")\)', m.group(4))
        nombre = (redes.get(red.group(1), red.group(1)) if red.group(1) else red.group(2)) if red else '?'
        vias.append((float(m.group(1)), float(m.group(2)), float(m.group(3)) / 2, nombre))
    return vias


VIAS_KICAD = vias_kicad(PCB)
# Bloques de soldadura (x, y en coordenadas del STEP)
SOLDADURA = {
    'sold_r5a': ('Soldadura R5 terminal 1', (46.6, 47.8), (-69.11, -65.91)),
    'sold_r5b': ('Soldadura R5 terminal 2', (51.7, 52.9), (-69.11, -65.91)),
    'sold_q1d': ('Soldadura Q1 pad de drenaje', (54.67, 59.83), (-69.71, -65.44)),
    'sold_u1ep': ('Soldadura U1 pad expuesto', (40.42, 44.08), (-70.75, -68.10)),
}
for i, x in enumerate((55.14, 56.41, 57.68, 58.95), 1):
    SOLDADURA[f'sold_q1p{i}'] = (f'Soldadura Q1 pata {i}', (x, x + 0.41), (-71.59, -70.98))

PARAMETROS = [
    ('Vin', '12[V]', 'Voltaje de entrada'),
    ('I_carga', '15[A]', 'Corriente en regimen (limite de corriente en estado estable)'),
    ('R_sense', '5[mohm]', 'R5, resistencia de deteccion (2512)'),
    ('Rds_on', '2.4[mohm]', 'Q1 Si7336ADP, Rds(on) tipico a 25 degC (datasheet)'),
    ('f_Rds', '1', 'Factor de Rds(on) por temperatura (1 = valor a 25 degC)'),
    ('I_q', '3[mA]', 'Corriente de alimentacion del LTC4215 (tipica, datasheet)'),
    ('P_R5', 'I_carga^2*R_sense', 'Perdidas en R5'),
    ('P_Q1', 'I_carga^2*Rds_on*f_Rds', 'Perdidas en Q1 (solo conduccion)'),
    ('P_U1', 'Vin*I_q', 'Perdidas en U1'),
    ('P_tot', 'P_R5+P_Q1+P_U1', 'Perdidas totales'),
    ('eps_rad', '0', 'Emisividad (0 = solo conveccion, como LM317/LM2596)'),
    ('T_amb', '25[degC]', 'Temperatura ambiente'),
    ('k_cu', '400[W/(m*K)]', 'Conductividad del cobre'),
    ('k_fr4', '0.3[W/(m*K)]', 'Conductividad del FR4'),
    ('t_cu', '35[um]', 'Espesor del cobre (1 oz)'),
    ('k_via', '122[W/(m*K)]', 'k equivalente de una via: barril de 25 um en taladro de 0.3 mm'),
    ('incluir_joule', '1', '1 = incluir el calentamiento I^2R del propio cobre (pistas y planos)'),
    ('sigma_cu0', '5.96e7[S/m]', 'Conductividad del cobre a 20 degC'),
    ('alpha_cu', '0.00393[1/K]', 'Coeficiente de temperatura de la resistividad del cobre'),
    ('sigma_min', '1[S/m]', 'Conductividad fuera del cobre (aislante; evita una matriz singular)'),
    ('k_air', '0.028[W/(m*K)]', 'Conductividad del aire (~50 degC)'),
    ('nu_air', '1.8e-5[m^2/s]', 'Viscosidad cinematica del aire'),
    ('alpha_air', '2.55e-5[m^2/s]', 'Difusividad termica del aire'),
    ('Pr_air', '0.71', 'Numero de Prandtl del aire'),
    ('L_h', '7.7[mm]', 'Longitud caracteristica horizontal (area/perimetro de la placa 74 x 19.5 mm)'),
    ('L_v', '5[mm]', 'Longitud caracteristica vertical'),
]
MATERIALES = [
    ('mat_fr4', 'FR4 (placa)', 'k_fr4', '1900', '1369'),
    ('mat_gen', 'Relleno generico (pasivos 0805, C4, D1, XT60)', '0.2', '1050', '1400'),
    ('mat_emc', 'EMC (cuerpos de Q1 y U1)', '0.7', '1900', '900'),
    ('mat_cu', 'Cobre (pad de drenaje y patas de Q1, pad expuesto y patas de U1)', 'k_cu', '8960', '385'),
    ('mat_r5', 'R5: ceramica de alumina (2512)', '25', '3900', '880'),
    ('mat_via', 'Vias: cobre equivalente', 'k_via', '8960', '385'),
    ('mat_sold', 'Soldadura SAC305', '58', '7400', '230'),
]
TEMPS = [('T max R5', 'max_R5(T)'), ('T union Q1 (cara pad-cuerpo)', 'max_fQ1(T)'), ('T max Q1', 'max_Q1(T)'),
         ('T max U1', 'max_U1(T)'), ('T max placa', 'max_PCB(T)')]
JOULE = [('P cobre F.Cu (I^2R)', 'int_F(Q_cobre)'), ('P cobre B.Cu (I^2R)', 'int_B(Q_cobre)')]


def caja(comp, tag, x, y, z, dim=2, condicion='intersects'):
    b = comp.selection().create(tag, 'Box')
    b.set('entitydim', str(dim))
    b.set('condition', condicion)
    for k, v in zip(('xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax'), (*x, *y, *z)):
        b.set(k, f'{v:g}')
    return tag


def geometria(comp):
    g = comp.geom().create('geom1', 3)
    g.lengthUnit('mm')
    imp = g.create('imp1', 'Import')
    imp.set('type', 'cad')
    imp.set('filename', str(GEO / 'EFUSE.step'))
    imp.set('unit', 'source')
    imp.importData()
    for tag, (etiqueta, x, y) in SOLDADURA.items():
        b = g.create(tag, 'Block')
        b.label(etiqueta)
        b.set('pos', [f'{x[0]:g}', f'{y[0]:g}', f'{Z_PCB:g}'])
        b.set('size', [f'{x[1] - x[0]:g}', f'{y[1] - y[0]:g}', f'{Z_COMP - Z_PCB:g}'])
    for i, (xk, yk, r, red) in enumerate(VIAS_KICAD, 1):
        c = g.create(f'via{i}', 'Cylinder')
        c.label(f'Via {i} ({red})')
        c.set('pos', [f'{xk:g}', f'{-yk:g}', '0'])
        c.set('r', f'{r:g}')
        c.set('h', f'{Z_PCB:g}')
    g.feature('fin').set('action', 'union')
    g.feature('fin').set('repairtoltype', 'relative')
    g.feature('fin').set('repairtol', '1e-8')
    g.run()
    return g


def clasificar(g) -> dict[int, tuple[str, str]]:
    mf = g.measureFinal()
    mf.selection().geom(3)
    tabla = {}
    dentro = lambda x0, x1, y0, y1, caja_: (x0 >= caja_[0] - 0.05 and x1 <= caja_[1] + 0.05
                                           and y0 >= caja_[2] - 0.05 and y1 <= caja_[3] + 0.05)
    R5 = (46.6, 52.9, -69.11, -65.91)
    Q1 = (54.67, 59.83, -71.59, -65.44)
    U1 = (39.75, 44.75, -71.43, -67.43)
    cerca = lambda a, b, tol=0.012: abs(a - b) < tol
    for d in range(1, g.getNDomains() + 1):
        mf.selection().set([d])
        x0, x1, y0, y1, z0, z1 = list(mf.getBoundingBox())
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        if dx > 70 and dy > 15:
            tabla[d] = ('mat_fr4', 'Placa')
        elif cerca(z0, Z_PCB) and cerca(z1, Z_COMP):
            tabla[d] = ('mat_sold', 'Soldadura')
        elif cerca(dz, Z_PCB, 0.02) and cerca(dx, 0.30, 0.03):
            tabla[d] = ('mat_via', 'Via')
        elif dentro(x0, x1, y0, y1, R5) and z0 > 1.5:
            tabla[d] = ('mat_r5', 'R5')
        elif dentro(x0, x1, y0, y1, Q1) and z0 > 1.5:
            tabla[d] = (('mat_cu', 'Q1 pad/patas') if z1 < 1.93 else ('mat_emc', 'Q1 cuerpo'))
        elif dentro(x0, x1, y0, y1, U1) and z0 > 1.5:
            tabla[d] = (('mat_cu', 'U1 pad/patas') if z1 < 1.80 else ('mat_emc', 'U1 cuerpo'))
        else:
            tabla[d] = ('mat_gen', f'Otro componente ({dx:.1f} x {dy:.1f} x {dz:.2f} mm)')
    return tabla


def materiales(comp, tabla):
    for tag, etiqueta, k, rho, cp in MATERIALES:
        m = comp.material().create(tag, 'Common')
        m.label(etiqueta)
        m.selection().set(sorted(d for d, (t, _) in tabla.items() if t == tag))
        pg = m.propertyGroup('def')
        pg.set('thermalconductivity', [k])
        pg.set('density', [rho])
        pg.set('heatcapacity', [cp])


def fisica(comp, j, tabla, ndom):
    for capa, archivo in (('mF', 'mascara_cobre_F.txt'), ('mB', 'mascara_cobre_B.txt')):
        f = j.func().create(capa, 'Interpolation')
        f.label(f'Mascara de cobre {capa[1]}.Cu (1 = cobre)')
        f.set('source', 'file')
        f.set('filename', str(GEO / archivo))
        f.set('struct', 'grid')
        f.importData()
        f.set('fununit', '1')
        f.set('extrap', 'const')

    todos = comp.selection().create('sel_todos', 'Explicit')
    todos.set(list(range(1, ndom + 1)))
    ext = comp.selection().create('sel_ext', 'Adjacent')
    ext.set('input', ['sel_todos'])
    caja(comp, 'sel_sup', (17, 92), (-80, -59), (Z_PCB - 0.004, Z_PCB + 0.004), condicion='inside')
    caja(comp, 'sel_inf', (17, 92), (-80, -59), (-0.004, 0.004), condicion='inside')
    r5 = sorted(d for d, (_, q) in tabla.items() if q == 'R5')
    s = comp.selection().create('sel_R5', 'Explicit')
    s.set(r5)
    # Caras horizontales completas dentro de una caja delgada (con 'intersects' tambien
    # entrarian las paredes laterales del pad)
    caja(comp, 'sel_qQ1', (54.6, 59.9), (-69.8, -65.4), (1.92, 1.93), condicion='inside')  # pad drenaje / cuerpo
    caja(comp, 'sel_qU1', (40.3, 44.2), (-70.9, -68.0), (1.79, 1.80), condicion='inside')  # pad expuesto / cuerpo
    for sel in ('sel_R5', 'sel_qQ1', 'sel_qU1'):
        n = [int(x) for x in comp.selection(sel).entities()]
        print(f'  {sel}: {n}', flush=True)
        assert n, f'{sel} quedo vacia'

    ht = comp.physics().create('ht', 'HeatTransfer', 'geom1')
    ht.feature('init1').set('Tinit', 'T_amb+20[K]')
    for tag, etiqueta, sel, mascara in (('tl_F', 'Cobre F.Cu (vertidos de potencia y pistas)', 'sel_sup', 'mF'),
                                        ('tl_B', 'Cobre B.Cu (plano GND)', 'sel_inf', 'mB')):
        tl = ht.create(tag, 'ThinLayer', 2)
        tl.label(etiqueta)
        tl.selection().named(sel)
        tl.set('LayerType', 'Conductive')
        tl.set('ds', 't_cu')
        tl.set('ks_mat', 'userdef')
        # la funcion recibe numeros sin unidad (mm): con [mm] COMSOL los pasa a metros
        k = f'k_fr4+(k_cu-k_fr4)*{mascara}(x/1[mm]-{X0_MASC},y/1[mm]+{Y0_MASC})'
        tl.set('ks', [k, '0', '0', '0', k, '0', '0', '0', k])
        for prop, val in (('rhos_mat', 'userdef'), ('rhos', '8960'), ('Cp_s_mat', 'userdef'), ('Cp_s', '385')):
            tl.set(prop, val)
    f = ht.create('q_R5', 'HeatSource', 3)
    f.label('R5: I^2 R en su volumen')
    f.selection().named('sel_R5')
    f.set('heatSourceType', 'HeatRate')
    f.set('P0', 'P_R5')
    for tag, etiqueta, sel, pot in (('q_Q1', 'Q1: die sobre el pad de drenaje', 'sel_qQ1', 'P_Q1'),
                                    ('q_U1', 'U1: die sobre el pad expuesto', 'sel_qU1', 'P_U1')):
        f = ht.create(tag, 'BoundaryHeatSource', 2)
        f.label(etiqueta)
        f.selection().named(sel)
        f.set('heatSourceType', 'HeatRate')
        f.set('Pb', pot)

    v = comp.variable().create('var_conv')
    v.label('Conveccion natural (correlaciones)')
    v.selection().geom('geom1', 2)
    v.selection().named('sel_ext')
    for nombre, expr in (
        ('dT', 'max(T-T_amb,0.1[K])'),
        ('beta_a', '2/(T+T_amb)'),
        ('Ra_h', 'g_const*beta_a*dT*L_h^3/(nu_air*alpha_air)'),
        ('Ra_v', 'g_const*beta_a*dT*L_v^3/(nu_air*alpha_air)'),
        ('h_arriba', 'k_air/L_h*if(Ra_h<=1e7,0.54*Ra_h^(1/4),0.15*Ra_h^(1/3))'),
        ('h_abajo', 'k_air/L_h*0.27*Ra_h^(1/4)'),
        ('h_vert', 'k_air/L_v*(0.825+0.387*Ra_v^(1/6)/(1+(0.492/Pr_air)^(9/16))^(8/27))^2'),
        ('h_conv', 'if(nz>0.7,h_arriba,if(nz<-0.7,h_abajo,h_vert))'),
    ):
        v.set(nombre, expr)
    h = ht.create('conv', 'HeatFluxBoundary', 2)
    h.label('Conveccion natural (todas las caras exteriores)')
    h.selection().named('sel_ext')
    h.set('HeatFluxType', 'ConvectiveHeatFlux')
    h.set('HeatTransferCoefficientType', 'UserDef')
    h.set('h', 'h_conv')
    h.set('Text_src', 'userdef')
    h.set('Text', 'T_amb')
    r = ht.create('rad', 'SurfaceToAmbientRadiation', 2)
    r.label('Radiacion al ambiente (apagada si eps_rad = 0)')
    r.selection().named('sel_ext')
    r.set('epsilon_rad_mat', 'userdef')
    r.set('epsilon_rad', 'eps_rad')
    r.set('Tamb_src', 'userdef')
    r.set('Tamb', 'T_amb')

    e = electrico(comp, j)
    q = ht.create('q_cobre', 'BoundaryHeatSource', 2)
    q.label('Calentamiento I^2R del cobre (pistas y planos)')
    q.selection().named('sel_cobre')
    q.set('heatSourceType', 'GeneralSource')
    q.set('Qb_input', 'incluir_joule*Q_cobre')
    comp.variable('var_elec').set('Q_cobre', 'ecis.Qsh', 'Calor por Joule por unidad de area')

    grupos = {'R5': r5,
              'Q1': sorted(d for d, (_, q) in tabla.items() if q.startswith('Q1')),
              'U1': sorted(d for d, (_, q) in tabla.items() if q.startswith('U1')),
              'PCB': sorted(d for d, (_, q) in tabla.items() if q == 'Placa')}
    for nombre, doms in grupos.items():
        op = comp.cpl().create(f'max_{nombre}', 'Maximum')
        op.selection().geom('geom1', 3)
        op.selection().set(doms)
    op = comp.cpl().create('max_fQ1', 'Maximum')
    op.selection().geom('geom1', 2)
    op.selection().named('sel_qQ1')


# Recorrido de los 15 A (coordenadas KiCad de los pads, verificadas en EFUSE.kicad_pcb):
#   +12V   (F.Cu): J1 pin 1 (23, 66.23)      -> R5 pad 2 (46.79, 67.51)
#   SENSE- (F.Cu): R5 pad 1 (52.71, 67.51)   -> pad de drenaje de Q1 (57.25, 67.82)
#   SOURCE (F.Cu): pines 1-3 de Q1 (y 71.18) -> J2 pin 1 (84.75, 65.91)
#   GND    (B.Cu): J2 pin 2 (84.75, 73.11)   -> J1 pin 2 (23, 73.43)
# En cada tramo se inyecta la corriente en un extremo (fuente distribuida sobre el area del
# pad, normalizada para sumar exactamente I_carga) y el otro extremo es tierra (una cara que
# existe en la geometria). El signo no importa para el calentamiento (I^2 R).
INYECCIONES = [  # (nombre, capa, centro x, centro y (STEP), semiancho x, semiancho y, signo)
    ('J1_mas', 'F', 23.0, -66.23, 3.0, 3.0, +1),
    ('R5_pad1', 'F', 52.71, -67.51, 0.62, 1.68, +1),
    ('J2_mas', 'F', 84.75, -65.91, 3.0, 3.0, +1),
    ('J2_gnd', 'B', 84.75, -73.11, 3.0, 3.0, +1),
    ('J1_gnd', 'B', 23.0, -73.43, 3.0, 3.0, -1),
]


def electrico(comp, j):
    """Corriente en las capas de cobre (Electric Currents, Shell) y su calentamiento I^2R."""
    u = comp.selection().create('sel_cobre', 'Union')
    u.set('entitydim', '2')
    u.set('input', ['sel_sup', 'sel_inf'])
    # Tierras: caras que existen en la geometria (bajo bloques de soldadura y una via de GND)
    caja(comp, 'gnd_R5pad2', (46.5, 47.9), (-69.2, -65.8), (Z_PCB - 0.004, Z_PCB + 0.004), condicion='inside')
    caja(comp, 'gnd_Q1d', (54.6, 59.9), (-69.8, -65.4), (Z_PCB - 0.004, Z_PCB + 0.004), condicion='inside')
    caja(comp, 'gnd_Q1s', (55.0, 58.2), (-71.7, -70.9), (Z_PCB - 0.004, Z_PCB + 0.004), condicion='inside')
    caja(comp, 'gnd_via', (42.0, 42.5), (-69.7, -69.2), (-0.004, 0.004), condicion='inside')
    for sel in ('gnd_R5pad2', 'gnd_Q1d', 'gnd_Q1s', 'gnd_via'):
        n = [int(x) for x in comp.selection(sel).entities()]
        print(f'  {sel}: caras {n}', flush=True)
        assert n, f'{sel} quedo vacia'

    # Indicadores de cada pad de inyeccion y su area efectiva (integral sobre la capa)
    v = comp.variable().create('var_elec')
    v.label('Corriente en el cobre: inyecciones y conductividad')
    sigma = 'sigma_cu0/(1+alpha_cu*(T-293.15[K]))'
    v.set('sigma_cobre', f'sigma_min+if(z>0.75[mm],{sigma}*mF(x/1[mm]-{X0_MASC},y/1[mm]+{Y0_MASC}),'
                         f'{sigma}*mB(x/1[mm]-{X0_MASC},y/1[mm]+{Y0_MASC}))')
    for capa, sel in (('F', 'sel_sup'), ('B', 'sel_inf')):
        op = comp.cpl().create(f'int_{capa}', 'Integration')
        op.selection().geom('geom1', 2)
        op.selection().named(sel)
    fuente = {'F': [], 'B': []}
    for nombre, capa, cx, cy, ax, ay, signo in INYECCIONES:
        # solo donde hay cobre (si no, la corriente entraria en el aislante)
        v.set(f'ind_{nombre}', f'(abs(x-({cx})[mm])<{ax}[mm])*(abs(y-({cy})[mm])<{ay}[mm])'
                               f'*m{capa}(x/1[mm]-{X0_MASC},y/1[mm]+{Y0_MASC})')
        fuente[capa].append(f'({signo})*I_carga*ind_{nombre}/(ecis.ds*int_{capa}(ind_{nombre}))')
    # El shell de COMSOL 6.4 no toma el espesor que se le da (usa ecis.ds = 0.1 mm): se escala la
    # conductividad para que la conductancia de hoja sigma*t_cu sea la correcta. Verificado contra
    # una tira con solucion analitica (P = I^2 R L / 3): coincide exacto.
    v.set('sigma_shell', 'sigma_cobre*t_cu/ecis.ds')

    e = comp.physics().create('ecis', 'ElectricCurrentsShell', 'geom1')
    e.label('Corriente de 15 A en las capas de cobre')
    e.selection().named('sel_cobre')
    csh = e.feature('csh1')
    for prop, val in (('sigma_crel_JcE_ElectricalConductivity_mat', 'userdef'),
                      ('sigma_crel_JcE_ElectricalConductivity', ['sigma_shell', '0', '0', '0', 'sigma_shell', '0', '0', '0', 'sigma_shell'])):
        csh.set(prop, val)
    for capa, sel in (('F', 'sel_sup'), ('B', 'sel_inf')):
        cs = e.create(f'cs_{capa}', 'CurrentSource', 2)
        cs.label(f'Inyeccion de corriente en {capa}.Cu (pads de entrada)')
        cs.selection().named(sel)
        cs.set('Qj', '+'.join(fuente[capa]))
    for sel in ('gnd_R5pad2', 'gnd_Q1d', 'gnd_Q1s', 'gnd_via'):
        gr = e.create(f'g_{sel}', 'Ground', 2)
        gr.label(f'Tierra: {sel}')
        gr.selection().named(sel)
    return e


def estudios(j):
    s = j.study().create('std1')
    s.label('1 Caso 12 V, 15 A')
    s.create('stat', 'Stationary')
    s = j.study().create('std2')
    s.label('2 Barrido de corriente')
    p = s.create('param', 'Parametric')
    p.set('pname', ['I_carga'])
    p.set('plistarr', ['5 7.5 10 12.5 15'])
    p.set('punit', ['A'])
    s.create('stat', 'Stationary')
    s = j.study().create('std3')
    s.label('3 Variantes a 15 A: Rds caliente, radiacion')
    p = s.create('param', 'Parametric')
    p.set('sweeptype', 'sparse')
    p.set('pname', ['f_Rds', 'eps_rad'])
    p.set('plistarr', ['1 1.5 1 1.5', '0 0 0.9 0.9'])
    p.set('punit', ['', ''])
    s.create('stat', 'Stationary')


def valor(model, expr, unidad, dset, k=None):
    kw = {'dataset': dset}
    if k is not None:
        kw['inner'] = [k]
    return float(np.real(np.ravel(model.evaluate(expr, unidad, **kw))[0]))


def exportar_figuras(model, j, comp, dset_tag, prefijo, titulo):
    tomas = [('isometrica', None, False, True),
             ('superior', ((54.5, -69.75, 300), (54.5, -69.75, 0), (0, 1, 0)), True, True),
             ('inferior', ((54.5, -69.75, -300), (54.5, -69.75, 0), (0, 1, 0)), True, True),
             ('acercamiento_R5_Q1', ((38, -100, 30), (51, -68, 1.5), (0, 0, 1)), False, False)]
    for nombre, camara, orto, ajustar in tomas:
        vt = f'v_{prefijo}_{nombre}'
        if vt in [str(t) for t in comp.view().tags()]:
            comp.view().remove(vt)
        v = comp.view().create(vt, 'geom1')
        cam = v.camera()
        if orto:
            cam.set('projection', 'orthographic')
        if camara:
            cam.set('position', [str(c) for c in camara[0]])
            cam.set('target', [str(c) for c in camara[1]])
            cam.set('up', [str(c) for c in camara[2]])
        tag = f'pg_{prefijo}_{nombre}'
        if tag in [str(t) for t in j.result().tags()]:
            j.result().remove(tag)
        pg = j.result().create(tag, 'PlotGroup3D')
        pg.set('data', dset_tag)
        pg.set('view', vt)
        pg.set('titletype', 'manual')
        pg.set('title', titulo)
        s = pg.create('surf1', 'Surface')
        s.set('expr', 'T')
        s.set('unit', 'degC')
        s.set('colortable', 'HeatCameraLight')
        e = j.result().export().create('img_tmp', 'Image')
        e.set('plotgroup', tag)
        e.set('filename', str(FIG / f'{prefijo}_{nombre}.png'))
        e.set('size', 'manualweb')
        e.set('width', '1600')
        e.set('height', '1000')
        e.set('zoomextents', 'on' if ajustar else 'off')
        e.set('options3d', 'on')
        for p in ('title3d', 'legend3d', 'axisorientation'):
            e.set(p, 'on')
        e.set('grid', 'off')
        e.set('background', 'color')
        try:
            e.run()
        finally:
            j.result().export().remove('img_tmp')


def main() -> int:
    for d in (RES, FIG):
        d.mkdir(parents=True, exist_ok=True)
    client = mph.start(cores=4)
    try:
        model = client.create('EFUSE_termico')
        j = model.java
        for n, e, desc in PARAMETROS:
            j.param().set(n, e, desc)
        comp = j.component().create('comp1', True)
        g = geometria(comp)
        print('Dominios:', g.getNDomains(), flush=True)
        tabla = clasificar(g)
        conteo = {}
        for t, _ in tabla.values():
            conteo[t] = conteo.get(t, 0) + 1
        print('Dominios por material:', conteo, flush=True)
        with (GEO / 'dominios_materiales.csv').open('w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['dominio', 'material', 'que es'])
            for d in sorted(tabla):
                w.writerow([d, tabla[d][0], tabla[d][1]])
        materiales(comp, tabla)
        fisica(comp, j, tabla, g.getNDomains())
        estudios(j)
        mesh = comp.mesh().create('mesh1')
        mesh.feature('size').set('hauto', '5')
        mesh.create('ftet1', 'FreeTet').set('zscale', str(ESCALA_Z))
        mesh.run()
        print('Malla:', mesh.getNumElem(), 'elementos, calidad min %.3f' % mesh.stat().getMinQuality(), flush=True)
        model.save(str(MODELO))

        # 1. Caso 12 V, 15 A
        model.solve('1 Caso 12 V, 15 A')
        model.save(str(MODELO))  # guardar antes de evaluar
        d1 = next(x for x in model / 'datasets' if x.tag() == 'dset1')
        filas = [(p, valor(model, p, 'W', d1), 'W') for p in ('P_R5', 'P_Q1', 'P_U1', 'P_tot')]
        filas += [(n, valor(model, e, 'W', d1), 'W') for n, e in JOULE]
        filas += [(n, valor(model, e, 'degC', d1), 'degC') for n, e in TEMPS]
        filas.append(('Cobre B.Cu: fraccion media de la capa (comprobacion de la mascara)', valor(model, 'int_B(mB(x/1[mm]-17.5,y/1[mm]+79.5))/int_B(1)', None, d1), ''))
        with (RES / 'caso_12V_15A.csv').open('w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['cantidad', 'valor', 'unidad'])
            for n, val, u in filas:
                w.writerow([n, f'{val:.4g}', u])
                print(f'  {n}: {val:.4g} {u}', flush=True)
        exportar_figuras(model, j, comp, 'dset1', '15A',
                         'Temperatura [degC]: eFuse LTC4215, 12 V, 15 A en regimen, solo conveccion, T amb 25 degC')
        model.save(str(MODELO))

        # 2. Barrido de corriente
        antes = {x.tag() for x in model / 'datasets'}
        model.solve('2 Barrido de corriente')
        model.save(str(MODELO))
        d2 = [x for x in model / 'datasets' if x.tag() not in antes][-1]
        corrientes = [5, 7.5, 10, 12.5, 15]
        with (RES / 'barrido_corriente.csv').open('w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['I [A]', 'P_R5 [W]', 'P_Q1 [W]', 'P_tot [W]'] + [f'{n} [degC]' for n, _ in TEMPS])
            for k, i in enumerate(corrientes, 1):
                fila = [i] + [f'{valor(model, p, "W", d2, k):.3f}' for p in ('P_R5', 'P_Q1', 'P_tot')] \
                       + [f'{valor(model, e, "degC", d2, k):.1f}' for _, e in TEMPS]
                w.writerow(fila)
                print('  I =', i, 'A ->', fila, flush=True)

        # 3. Variantes
        antes = {x.tag() for x in model / 'datasets'}
        model.solve('3 Variantes a 15 A: Rds caliente, radiacion')
        model.save(str(MODELO))
        d3 = [x for x in model / 'datasets' if x.tag() not in antes][-1]
        nombres = ['Rds(on) a 25 degC, solo conveccion (base)', 'Rds(on) x1.5 (caliente), solo conveccion',
                   'Rds(on) a 25 degC + radiacion (eps 0.9)', 'Rds(on) x1.5 + radiacion']
        with (RES / 'variantes_15A.csv').open('w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['variante', 'P_Q1 [W]'] + [f'{n} [degC]' for n, _ in TEMPS])
            for k, nombre in enumerate(nombres, 1):
                fila = [nombre, f'{valor(model, "P_Q1", "W", d3, k):.3f}'] \
                       + [f'{valor(model, e, "degC", d3, k):.1f}' for _, e in TEMPS]
                w.writerow(fila)
                print(' ', fila, flush=True)
        print('Guardado:', MODELO)
        return 0
    finally:
        client.disconnect()


if __name__ == '__main__':
    raise SystemExit(main())
