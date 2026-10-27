"""
Ponto de entrada usado pelo Agendador de Tarefas do Windows.

Roda sem abrir janela (pythonw) e grava tudo em log.txt, na mesma pasta.
O log é recomeçado quando passa de 1 MB.
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
LOG = PASTA / "log.txt"

if LOG.exists() and LOG.stat().st_size > 1_000_000:
    LOG.replace(PASTA / "log-anterior.txt")

with open(LOG, "a", encoding="utf-8") as saida:
    sys.stdout = sys.stderr = saida
    print(f"\n########## {datetime.now():%d/%m/%Y %H:%M} ##########", flush=True)
    try:
        sys.path.insert(0, str(PASTA))
        import monitor

        monitor.main()
    except Exception:
        traceback.print_exc()
    saida.flush()
