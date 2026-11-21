"""Ejecuta la extracción y después la clasificación de publicaciones."""

import argparse
import subprocess
import sys
from pathlib import Path

from social_media.label_social_media import load_config


ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Ejecutar ambas fases; escribe en MongoDB y llama a Azure")
    parser.add_argument("--account", choices=("laura_fernandez_delgado", "presidenciacr"),
                        action="append", help="Limitar la extracción a perfiles específicos")
    parser.add_argument("--max-posts", type=int, default=0,
                        help="Limitar publicaciones de fase 1; 0 sin límite")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--retry-unresolved", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--relabel", action="store_true",
                        help="Reclasificar también las etiquetas existentes de toda la colección")
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args(argv)
    if args.max_posts < 0:
        parser.error("--max-posts debe ser mayor o igual a cero")

    phase2 = [sys.executable, "-u", "-m", "social_media.label_social_media"]
    if args.relabel:
        phase2.append("--relabel")
    if args.checkpoint:
        phase2.extend(["--checkpoint", str(args.checkpoint)])
    try:
        config = load_config(ROOT)
    except RuntimeError as error:
        print(f"Configuración inválida: {error}", file=sys.stderr)
        return 1
    if config["MONGODB_DB"] != "SocialMediaDB":
        print("El pipeline requiere MONGODB_DB=SocialMediaDB para ambas fases.", file=sys.stderr)
        return 1

    if not args.write:
        print("Vista previa: fase 1 no se ejecuta; fase 2 solo inspecciona la base.", flush=True)
        phase2.append("--dry-run")
        return subprocess.run(phase2, cwd=ROOT, check=False).returncode

    phase1 = [sys.executable, "-u", "-m", "social_media.batch", "--max-posts", str(args.max_posts)]
    for account in args.account or []:
        phase1.extend(["--account", account])
    for flag in ("skip_discovery", "retry_errors", "retry_unresolved", "headed"):
        if getattr(args, flag):
            phase1.append("--" + flag.replace("_", "-"))
    print("Fase 1: extracción y almacenamiento.", flush=True)
    result = subprocess.run(phase1, cwd=ROOT, check=False)
    if result.returncode:
        print("Fase 1 falló; fase 2 no se ejecutará.", file=sys.stderr, flush=True)
        return result.returncode
    print("Fase 2: clasificación de la colección completa.", flush=True)
    return subprocess.run(phase2, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
