"""Mascaras de cobre (F.Cu y B.Cu) de la placa para el modelo termico.

Exporta cada capa de cobre con kicad-cli a SVG (solo el area de la placa), la rasteriza a
0.1 mm por pixel y escribe un archivo de interpolacion 'grid' de COMSOL con la fraccion de
cobre (0 a 1).

Uso:  python mascaras_cobre.py [archivo.kicad_pcb] [carpeta_salida]
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

FEM = Path(__file__).resolve().parent.parent
PCB = FEM.parent / 'kicad' / 'EFUSE' / 'EFUSE.kicad_pcb'
X0, Y0 = 17.5, 79.5          # esquina inferior izquierda de la placa (coordenadas KiCad)
ANCHO, ALTO = 74.0, 19.5     # mm
PASO = 0.1                   # mm por pixel


def capa(pcb: Path, nombre: str, tmp: Path) -> np.ndarray:
    svg = tmp / f'{nombre}.svg'
    subprocess.run(['kicad-cli', 'pcb', 'export', 'svg', '--mode-single', '-l', nombre, '--page-size-mode', '2',
                    '--exclude-drawing-sheet', '--black-and-white', '--drill-shape-opt', '0',
                    '-o', str(svg), str(pcb)], check=True, capture_output=True)
    nx, ny = round(ANCHO / PASO), round(ALTO / PASO)
    png = subprocess.run(['rsvg-convert', '-w', str(nx), '-h', str(ny), '-b', 'white', str(svg)],
                         check=True, capture_output=True).stdout
    gris = np.asarray(Image.open(io.BytesIO(png)).convert('L'), dtype=float)
    return 1.0 - gris / 255.0  # negro = cobre


def escribir(frac: np.ndarray, nombre: str, archivo: Path) -> None:
    ny, nx = frac.shape
    xs = (np.arange(nx) + 0.5) * PASO
    ys = (np.arange(ny) + 0.5) * PASO
    datos = frac[::-1]  # fila 0 de la imagen = borde superior; en el archivo, Y crece hacia arriba
    with archivo.open('w') as f:
        f.write(f'% Fraccion de cobre de la capa {nombre} (1 = cobre). mm, origen en la esquina inferior izquierda '
                f'de la placa (X = x_KiCad - {X0}, Y = {Y0} - y_KiCad)\n%Grid\n')
        f.write(' '.join(f'{x:.4g}' for x in xs) + '\n')
        f.write(' '.join(f'{y:.4g}' for y in ys) + '\n%Data\n')
        for fila in datos:
            f.write(' '.join(f'{v:.3g}' for v in fila) + '\n')


def main() -> int:
    pcb = Path(sys.argv[1]) if len(sys.argv) > 1 else PCB
    salida = Path(sys.argv[2]) if len(sys.argv) > 2 else FEM / 'geometria'
    with tempfile.TemporaryDirectory() as t:
        for nombre, archivo in (('F.Cu', 'mascara_cobre_F.txt'), ('B.Cu', 'mascara_cobre_B.txt')):
            frac = capa(pcb, nombre, Path(t))
            escribir(frac, nombre, salida / archivo)
            print(f'{nombre}: {frac.shape[1]}x{frac.shape[0]} px, cobre {100 * frac.mean():.1f} %')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
