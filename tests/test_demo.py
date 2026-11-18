"""A demo do núcleo roda do começo ao fim, mesmo com a saída em pipe num console cp1252."""

import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def test_demo_roda_com_saida_em_pipe_num_console_cp1252():
    ambiente = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    r = subprocess.run([sys.executable, "demo.py"], cwd=RAIZ, env=ambiente,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    saida = r.stdout.decode("utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "→" in saida  # o caractere que o cp1252 não tem
    assert "COMPROMISSO VÁLIDO" in saida and "COMPROMISSO INVÁLIDO" in saida
